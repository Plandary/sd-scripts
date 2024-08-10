import os
import glob
from typing import Any, List, Optional, Tuple, Union
import torch
import numpy as np
from transformers import CLIPTokenizer, T5TokenizerFast

from library import sd3_utils, train_util
from library import sd3_models
from library.strategy_base import LatentsCachingStrategy, TextEncodingStrategy, TokenizeStrategy, TextEncoderOutputsCachingStrategy

from library.utils import setup_logging

setup_logging()
import logging

logger = logging.getLogger(__name__)


CLIP_L_TOKENIZER_ID = "openai/clip-vit-large-patch14"
T5_XXL_TOKENIZER_ID = "google/t5-v1_1-xxl"


class FluxTokenizeStrategy(TokenizeStrategy):
    def __init__(self, t5xxl_max_length: int = 256, tokenizer_cache_dir: Optional[str] = None) -> None:
        self.t5xxl_max_length = t5xxl_max_length
        self.clip_l = self._load_tokenizer(CLIPTokenizer, CLIP_L_TOKENIZER_ID, tokenizer_cache_dir=tokenizer_cache_dir)
        self.t5xxl = self._load_tokenizer(T5TokenizerFast, T5_XXL_TOKENIZER_ID, tokenizer_cache_dir=tokenizer_cache_dir)

    def tokenize(self, text: Union[str, List[str]]) -> List[torch.Tensor]:
        text = [text] if isinstance(text, str) else text

        l_tokens = self.clip_l(text, max_length=77, padding="max_length", truncation=True, return_tensors="pt")
        t5_tokens = self.t5xxl(text, max_length=self.t5xxl_max_length, padding="max_length", truncation=True, return_tensors="pt")

        t5_attn_mask = t5_tokens["attention_mask"]
        l_tokens = l_tokens["input_ids"]
        t5_tokens = t5_tokens["input_ids"]

        return [l_tokens, t5_tokens, t5_attn_mask]


class FluxTextEncodingStrategy(TextEncodingStrategy):
    def __init__(self) -> None:
        pass

    def encode_tokens(
        self,
        tokenize_strategy: TokenizeStrategy,
        models: List[Any],
        tokens: List[torch.Tensor],
        apply_t5_attn_mask: bool = False,
    ) -> List[torch.Tensor]:
        # supports single model inference only

        clip_l, t5xxl = models
        l_tokens, t5_tokens = tokens[:2]
        t5_attn_mask = tokens[2] if len(tokens) > 2 else None

        if clip_l is not None and l_tokens is not None:
            l_pooled = clip_l(l_tokens.to(clip_l.device))["pooler_output"]
        else:
            l_pooled = None

        if t5xxl is not None and t5_tokens is not None:
            # t5_out is [b, max length, 4096]
            t5_out, _ = t5xxl(t5_tokens.to(t5xxl.device), return_dict=False, output_hidden_states=True)
            if apply_t5_attn_mask:
                t5_out = t5_out * t5_attn_mask.to(t5_out.device).unsqueeze(-1)
            txt_ids = torch.zeros(t5_out.shape[0], t5_out.shape[1], 3, device=t5_out.device)
        else:
            t5_out = None
            txt_ids = None

        return [l_pooled, t5_out, txt_ids]


class FluxTextEncoderOutputsCachingStrategy(TextEncoderOutputsCachingStrategy):
    FLUX_TEXT_ENCODER_OUTPUTS_NPZ_SUFFIX = "_flux_te.npz"

    def __init__(
        self,
        cache_to_disk: bool,
        batch_size: int,
        skip_disk_cache_validity_check: bool,
        is_partial: bool = False,
        apply_t5_attn_mask: bool = False,
    ) -> None:
        super().__init__(cache_to_disk, batch_size, skip_disk_cache_validity_check, is_partial)
        self.apply_t5_attn_mask = apply_t5_attn_mask

    def get_outputs_npz_path(self, image_abs_path: str) -> str:
        return os.path.splitext(image_abs_path)[0] + FluxTextEncoderOutputsCachingStrategy.FLUX_TEXT_ENCODER_OUTPUTS_NPZ_SUFFIX

    def is_disk_cached_outputs_expected(self, npz_path: str):
        if not self.cache_to_disk:
            return False
        if not os.path.exists(npz_path):
            return False
        if self.skip_disk_cache_validity_check:
            return True

        try:
            npz = np.load(npz_path)
            if "l_pooled" not in npz:
                return False
            if "t5_out" not in npz:
                return False
            if "txt_ids" not in npz:
                return False
        except Exception as e:
            logger.error(f"Error loading file: {npz_path}")
            raise e

        return True

    def mask_t5_attn(self, t5_out: np.ndarray, t5_attn_mask: np.ndarray) -> np.ndarray:
        return t5_out * np.expand_dims(t5_attn_mask, -1)

    def load_outputs_npz(self, npz_path: str) -> List[np.ndarray]:
        data = np.load(npz_path)
        l_pooled = data["l_pooled"]
        t5_out = data["t5_out"]
        txt_ids = data["txt_ids"]

        if self.apply_t5_attn_mask:
            t5_attn_mask = data["t5_attn_mask"]
            t5_out = self.mask_t5_attn(t5_out, t5_attn_mask)

        return [l_pooled, t5_out, txt_ids]

    def cache_batch_outputs(
        self, tokenize_strategy: TokenizeStrategy, models: List[Any], text_encoding_strategy: TextEncodingStrategy, infos: List
    ):
        flux_text_encoding_strategy: FluxTextEncodingStrategy = text_encoding_strategy
        captions = [info.caption for info in infos]

        tokens_and_masks = tokenize_strategy.tokenize(captions)
        with torch.no_grad():
            l_pooled, t5_out, txt_ids = flux_text_encoding_strategy.encode_tokens(
                tokenize_strategy, models, tokens_and_masks, self.apply_t5_attn_mask
            )

        if l_pooled.dtype == torch.bfloat16:
            l_pooled = l_pooled.float()
        if t5_out.dtype == torch.bfloat16:
            t5_out = t5_out.float()
        if txt_ids.dtype == torch.bfloat16:
            txt_ids = txt_ids.float()

        l_pooled = l_pooled.cpu().numpy()
        t5_out = t5_out.cpu().numpy()
        txt_ids = txt_ids.cpu().numpy()

        for i, info in enumerate(infos):
            l_pooled_i = l_pooled[i]
            t5_out_i = t5_out[i]
            txt_ids_i = txt_ids[i]

            if self.cache_to_disk:
                t5_attn_mask = tokens_and_masks[2]
                t5_attn_mask_i = t5_attn_mask[i].cpu().numpy()
                np.savez(
                    info.text_encoder_outputs_npz,
                    l_pooled=l_pooled_i,
                    t5_out=t5_out_i,
                    txt_ids=txt_ids_i,
                    t5_attn_mask=t5_attn_mask_i,
                )
            else:
                info.text_encoder_outputs = (l_pooled_i, t5_out_i, txt_ids_i)


class FluxLatentsCachingStrategy(LatentsCachingStrategy):
    FLUX_LATENTS_NPZ_SUFFIX = "_flux.npz"

    def __init__(self, cache_to_disk: bool, batch_size: int, skip_disk_cache_validity_check: bool) -> None:
        super().__init__(cache_to_disk, batch_size, skip_disk_cache_validity_check)

    def get_image_size_from_disk_cache_path(self, absolute_path: str) -> Tuple[Optional[int], Optional[int]]:
        npz_file = glob.glob(os.path.splitext(absolute_path)[0] + "_*" + FluxLatentsCachingStrategy.FLUX_LATENTS_NPZ_SUFFIX)
        if len(npz_file) == 0:
            return None, None
        w, h = os.path.splitext(npz_file[0])[0].split("_")[-2].split("x")
        return int(w), int(h)

    def get_latents_npz_path(self, absolute_path: str, image_size: Tuple[int, int]) -> str:
        return (
            os.path.splitext(absolute_path)[0]
            + f"_{image_size[0]:04d}x{image_size[1]:04d}"
            + FluxLatentsCachingStrategy.FLUX_LATENTS_NPZ_SUFFIX
        )

    def is_disk_cached_latents_expected(self, bucket_reso: Tuple[int, int], npz_path: str, flip_aug: bool, alpha_mask: bool):
        return self._default_is_disk_cached_latents_expected(8, bucket_reso, npz_path, flip_aug, alpha_mask)

    # TODO remove circular dependency for ImageInfo
    def cache_batch_latents(self, vae, image_infos: List, flip_aug: bool, alpha_mask: bool, random_crop: bool):
        encode_by_vae = lambda img_tensor: vae.encode(img_tensor).to("cpu")
        vae_device = vae.device
        vae_dtype = vae.dtype

        self._default_cache_batch_latents(encode_by_vae, vae_device, vae_dtype, image_infos, flip_aug, alpha_mask, random_crop)

        if not train_util.HIGH_VRAM:
            train_util.clean_memory_on_device(vae.device)


if __name__ == "__main__":
    # test code for FluxTokenizeStrategy
    # tokenizer = sd3_models.SD3Tokenizer()
    strategy = FluxTokenizeStrategy(256)
    text = "hello world"

    l_tokens, g_tokens, t5_tokens = strategy.tokenize(text)
    # print(l_tokens.shape)
    print(l_tokens)
    print(g_tokens)
    print(t5_tokens)

    texts = ["hello world", "the quick brown fox jumps over the lazy dog"]
    l_tokens_2 = strategy.clip_l(texts, max_length=77, padding="max_length", truncation=True, return_tensors="pt")
    g_tokens_2 = strategy.clip_g(texts, max_length=77, padding="max_length", truncation=True, return_tensors="pt")
    t5_tokens_2 = strategy.t5xxl(
        texts, max_length=strategy.t5xxl_max_length, padding="max_length", truncation=True, return_tensors="pt"
    )
    print(l_tokens_2)
    print(g_tokens_2)
    print(t5_tokens_2)

    # compare
    print(torch.allclose(l_tokens, l_tokens_2["input_ids"][0]))
    print(torch.allclose(g_tokens, g_tokens_2["input_ids"][0]))
    print(torch.allclose(t5_tokens, t5_tokens_2["input_ids"][0]))

    text = ",".join(["hello world! this is long text"] * 50)
    l_tokens, g_tokens, t5_tokens = strategy.tokenize(text)
    print(l_tokens)
    print(g_tokens)
    print(t5_tokens)

    print(f"model max length l: {strategy.clip_l.model_max_length}")
    print(f"model max length g: {strategy.clip_g.model_max_length}")
    print(f"model max length t5: {strategy.t5xxl.model_max_length}")