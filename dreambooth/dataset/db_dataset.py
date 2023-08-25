import copy
import logging
import os.path
import random
import traceback
from typing import List, Tuple, Union

import safetensors.torch
import torch.utils.data
from PIL.Image import Image
from torchvision.transforms import transforms
from transformers import CLIPTokenizer

from dreambooth import shared
from dreambooth.dataclasses.prompt_data import PromptData
from dreambooth.shared import status
from dreambooth.utils.image_utils import make_bucket_resolutions, \
    closest_resolution, shuffle_tags, open_and_trim
from dreambooth.utils.text_utils import build_strict_tokens
from helpers.mytqdm import mytqdm

logger = logging.getLogger(__name__)


class DbDataset(torch.utils.data.Dataset):
    """
    Dataset for handling training data
    """

    def __init__(
            self,
            batch_size: int,
            instance_prompts: List[PromptData],
            class_prompts: List[PromptData],
            tokens: List[Tuple[str, str]],
            tokenizer: Union[CLIPTokenizer, List[CLIPTokenizer], None],
            text_encoder,
            accelerator,
            resolution: int,
            hflip: bool,
            shuffle_tags: bool,
            strict_tokens: bool,
            dynamic_img_norm: bool,
            not_pad_tokens: bool,
            max_token_length: int,
            debug_dataset: bool,
            model_dir: str,
            pbar: mytqdm = None
    ) -> None:
        super().__init__()
        self.batch_indices = []
        self.batch_samples = []
        self.class_count = 0
        self.max_token_length = max_token_length
        self.cache_dir = os.path.join(model_dir, "cache")
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        print("Init dataset!")
        # A dictionary of string/latent pairs matching image paths
        self.latents_cache = {}
        # A dictionary of string/input_ids(s) pairs matching image paths
        self.caption_cache = {}
        # An optional dictionary of string/(input_id, addtl_kwargs) for SDXL
        self.sdxl_cache = {}
        # A dictionary of (int, int) / List[(string, string)] of resolutions and the corresponding image paths/captions
        self.train_dict = {}
        # A dictionary of (int, int) / List[(string, string)] of resolutions and the corresponding image paths/captions
        self.class_dict = {}
        # A mash-up of the class/train dicts that is perfectly fair and balanced.
        self.sample_dict = {}
        # This is where we just keep a list of everything for batching
        self.sample_cache = []
        # This is just a list of the sample names that we can use to find where in the cache an image is
        self.sample_indices = []
        # All of the available bucket resolutions
        self.resolutions = []
        # Currently active resolution
        self.active_resolution = (0, 0)
        # The currently active image index while iterating
        self.image_index = 0
        # Total len of the dataloader
        self._length = 0
        self.batch_size = batch_size
        self.batch_sampler = torch.utils.data.BatchSampler(self, batch_size, drop_last=True)
        self.train_img_data = instance_prompts
        self.class_img_data = class_prompts
        self.num_train_images = len(self.train_img_data)
        self.num_class_images = len(self.class_img_data)

        self.tokenizers = []
        if isinstance(tokenizer, CLIPTokenizer):
            self.tokenizers = [tokenizer]
        elif isinstance(tokenizer, list):
            self.tokenizers = tokenizer
        self.text_encoders = text_encoder
        self.accelerator = accelerator
        self.resolution = resolution
        self.debug_dataset = debug_dataset
        self.shuffle_tags = shuffle_tags
        self.not_pad_tokens = not_pad_tokens
        self.strict_tokens = strict_tokens
        self.dynamic_img_norm = dynamic_img_norm
        self.tokens = tokens
        self.vae = None
        self.pbar = pbar
        self.cache_latents = False
        flip_p = 0.5 if hflip else 0.0
        self.image_transforms = self.build_compose(hflip, flip_p)

    def build_compose(self, hflip, flip_p):
        img_augmentation = [transforms.ToPILImage(), transforms.RandomHorizontalFlip(flip_p)]
        to_tensor = [transforms.ToTensor()]

        image_transforms = (
            to_tensor if not hflip else img_augmentation + to_tensor
        )
        return transforms.Compose(image_transforms)

    def get_img_std(self, img):
        if self.dynamic_img_norm:
            return img.mean(), img.std()
        else:
            return [0.5], [0.5]

    def image_transform(self, img):
        img = self.image_transforms(img)
        mean, std = self.get_img_std(img)
        norm = transforms.Normalize(mean, std)
        return norm(img)

    def encode_prompt(self, prompt):
        prompt_embeds_list = []
        pooled_prompt_embeds = None  # default declaration
        bs_embed = None  # default declaration

        auto_add_special_tokens = False if self.strict_tokens else True
        if self.shuffle_tags:
            prompt = shuffle_tags(prompt)
        for tokenizer, text_encoder in zip(self.tokenizers, self.text_encoders):
            if self.strict_tokens:
                prompt = build_strict_tokens(prompt, tokenizer.bos_token, tokenizer.eos_token)

            b_size = 1  # as we are working with a single prompt
            n_size = 1 if self.max_token_length is None else self.max_token_length // 75

            text_inputs = tokenizer(
                prompt,
                padding="max_length",
                max_length=tokenizer.model_max_length,
                truncation=True,
                add_special_tokens=auto_add_special_tokens,
                return_tensors="pt",
            )
            text_input_ids = text_inputs.input_ids.view(-1,
                                                        tokenizer.model_max_length)  # reshape to handle different token lengths

            untruncated_ids = tokenizer(prompt, padding="longest", return_tensors="pt").input_ids

            if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(text_input_ids,
                                                                                         untruncated_ids):
                removed_text = tokenizer.batch_decode(untruncated_ids[:, tokenizer.model_max_length - 1: -1])
                logger.warning(
                    "The following part of your input was truncated because the model can only handle sequences up to"
                    f" {tokenizer.model_max_length} tokens: {removed_text}"
                )

            enc_out = text_encoder(
                text_input_ids.to(text_encoder.device),
                output_hidden_states=True,
                return_dict=True
            )

            # get hidden states and handle reshaping
            prompt_embeds = enc_out["hidden_states"][-2]  # penuultimate layer
            prompt_embeds = prompt_embeds.reshape(
                (b_size, -1, prompt_embeds.shape[-1]))  # reshape to handle different token lengths

            # handle varying max token lengths
            if self.max_token_length is not None:
                states_list = [prompt_embeds[:, 0].unsqueeze(1)]
                for i in range(1, self.max_token_length, tokenizer.model_max_length):
                    states_list.append(prompt_embeds[:, i: i + tokenizer.model_max_length - 2])
                states_list.append(prompt_embeds[:, -1].unsqueeze(1))
                prompt_embeds = torch.cat(states_list, dim=1)

            # We are only ALWAYS interested in the pooled output of the final text encoder
            pooled_prompt_embeds = enc_out["text_embeds"]
            if self.max_token_length is not None:
                pooled_prompt_embeds = pooled_prompt_embeds[::n_size]

            bs_embed, seq_len, _ = prompt_embeds.shape
            prompt_embeds = prompt_embeds.view(bs_embed, seq_len, -1)
            prompt_embeds_list.append(prompt_embeds)

        prompt_embeds = torch.cat(prompt_embeds_list, dim=-1)
        pooled_prompt_embeds = pooled_prompt_embeds.view(bs_embed, -1)
        return prompt_embeds, pooled_prompt_embeds

    def encode_prompt_og(self, prompt):
        prompt_embeds_list = []
        pooled_prompt_embeds = None  # default declaration
        bs_embed = None  # default declaration

        auto_add_special_tokens = False if self.strict_tokens else True
        if self.shuffle_tags:
            prompt = shuffle_tags(prompt)
        for tokenizer, text_encoder in zip(self.tokenizers, self.text_encoders):
            if self.strict_tokens:
                prompt = build_strict_tokens(prompt, tokenizer.bos_token, tokenizer.eos_token)

            text_inputs = tokenizer(
                prompt,
                padding="max_length",
                max_length=tokenizer.model_max_length,
                truncation=True,
                add_special_tokens=auto_add_special_tokens,
                return_tensors="pt",
            )
            text_input_ids = text_inputs.input_ids
            untruncated_ids = tokenizer(prompt, padding="longest", return_tensors="pt").input_ids

            if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(text_input_ids,
                                                                                         untruncated_ids):
                removed_text = tokenizer.batch_decode(untruncated_ids[:, tokenizer.model_max_length - 1: -1])
                logger.warning(
                    "The following part of your input was truncated because CLIP can only handle sequences up to"
                    f" {tokenizer.model_max_length} tokens: {removed_text}"
                )

            prompt_embeds = text_encoder(
                text_input_ids.to(text_encoder.device),
                output_hidden_states=True,
            )

            # We are only ALWAYS interested in the pooled output of the final text encoder
            pooled_prompt_embeds = prompt_embeds[0]
            prompt_embeds = prompt_embeds.hidden_states[-2]
            bs_embed, seq_len, _ = prompt_embeds.shape
            prompt_embeds = prompt_embeds.view(bs_embed, seq_len, -1)
            prompt_embeds_list.append(prompt_embeds)

        prompt_embeds = torch.concat(prompt_embeds_list, dim=-1)
        pooled_prompt_embeds = pooled_prompt_embeds.view(bs_embed, -1)
        return prompt_embeds, pooled_prompt_embeds

    def compute_embeddings(self, reso, prompt):
        original_size = reso
        target_size = reso
        crops_coords_top_left = (0, 0)
        with torch.no_grad():
            prompt_embeds, pooled_prompt_embeds = self.encode_prompt(prompt)
            add_text_embeds = pooled_prompt_embeds

            # Adapted from pipeline.StableDiffusionXLPipeline._get_add_time_ids
            add_time_ids = list(original_size + crops_coords_top_left + target_size)
            add_time_ids = torch.tensor([add_time_ids])

            prompt_embeds = prompt_embeds.to(self.accelerator.device)
            add_text_embeds = add_text_embeds.to(self.accelerator.device)
            add_time_ids = add_time_ids.to(self.accelerator.device, dtype=prompt_embeds.dtype)
            unet_added_cond_kwargs = {"text_embeds": add_text_embeds, "time_ids": add_time_ids}
        return prompt_embeds, unet_added_cond_kwargs

    def load_image(self, image_path, caption, res):
        input_ids_2 = None
        if self.debug_dataset:
            image = os.path.splitext(image_path)
            input_ids = caption
        else:
            if self.cache_latents:
                image = self.latents_cache[image_path]
            else:
                img = open_and_trim(image_path, res, False)
                image = self.image_transform(img)
            if self.shuffle_tags:
                caption, input_ids = self.cache_caption(image_path, caption)
            else:
                input_ids = self.caption_cache[image_path]
        return image, input_ids

    def cache_latent(self, image_path, res):
        if self.vae is not None:
            image = open_and_trim(image_path, res, False)
            img_tensor = self.image_transform(image)
            img_tensor = img_tensor.unsqueeze(0).to(device=self.vae.device, dtype=self.vae.dtype)
            latents = self.vae.encode(img_tensor).latent_dist.sample().squeeze(0).to("cpu")
            self.latents_cache[image_path] = latents

    def cache_caption(self, image_path, caption):
        input_ids = None
        auto_add_special_tokens = False if self.strict_tokens else True
        if len(self.tokenizers) > 0 and (image_path not in self.caption_cache or self.debug_dataset):
            if self.shuffle_tags:
                caption = shuffle_tags(caption)
            if self.strict_tokens:
                caption = build_strict_tokens(caption, self.tokenizers[0].bos_token, self.tokenizers[0].eos_token)
            if self.not_pad_tokens:
                input_ids = self.tokenizers[0](caption, padding=True, truncation=True,
                                               add_special_tokens=auto_add_special_tokens,
                                               return_tensors="pt").input_ids
            else:
                input_ids = self.tokenizers[0](caption, padding='max_length', truncation=True,
                                               add_special_tokens=auto_add_special_tokens,
                                               return_tensors='pt').input_ids
            if not self.shuffle_tags:
                self.caption_cache[image_path] = input_ids

        return caption, input_ids

    def make_buckets_with_caching(self, vae):
        self.vae = vae
        self.cache_latents = vae is not None
        state = f"Preparing Dataset ({'With Caching' if self.cache_latents else 'Without Caching'})"
        print(state)
        if self.pbar is not None:
            self.pbar.set_description(state)
        status.textinfo = state

        # Create a list of resolutions
        bucket_resos = make_bucket_resolutions(self.resolution)
        self.train_dict = {}

        def sort_images(img_data: List[PromptData], resos, target_dict, is_class_img):
            for prompt_data in img_data:
                path = prompt_data.src_image
                image_width, image_height = prompt_data.resolution
                cap = prompt_data.prompt
                reso = closest_resolution(image_width, image_height, resos)
                concept_idx = prompt_data.concept_index
                # Append the concept index to the resolution, and boom, we got ourselves split concepts.
                di = (*reso, concept_idx)
                target_dict.setdefault(di, []).append((path, cap, is_class_img))

        sort_images(self.train_img_data, bucket_resos, self.train_dict, False)
        sort_images(self.class_img_data, bucket_resos, self.class_dict, True)

        def cache_images(images, reso, p_bar: mytqdm):
            for img_path, cap, is_prior in images:
                try:
                    # If the image is not in the "precache",cache it
                    if img_path not in latents_cache:
                        if self.cache_latents and not self.debug_dataset:
                            self.cache_latent(img_path, reso)

                    if len(self.tokenizers) == 2 and img_path not in self.sdxl_cache:
                        foo1, foo2 = self.compute_embeddings(reso, cap)
                        self.sdxl_cache[img_path] = (foo1, foo2)
                    # Otherwise, load it from existing cache
                    else:
                        self.latents_cache[img_path] = latents_cache[img_path]
                    if not self.shuffle_tags:
                        self.cache_caption(img_path, cap)
                    self.sample_indices.append(img_path)
                    self.sample_cache.append((img_path, cap, is_prior))
                    p_bar.update()
                except Exception as e:
                    traceback.print_exc()
                    print(f"Exception caching: {img_path}: {e}")
                    if img_path in self.caption_cache:
                        del self.caption_cache[img_path]
                    if (img_path, cap, is_prior) in self.sample_cache:
                        del self.sample_cache[(img_path, cap, is_prior)]
                    if img_path in self.sample_indices:
                        del self.sample_indices[img_path]
                    if img_path in self.latents_cache:
                        del self.latents_cache[img_path]
            self.latents_cache.update(latents_cache)

        bucket_idx = 0
        total_len = 0
        bucket_len = {}
        max_idx_chars = len(str(len(self.train_dict.keys())))
        p_len = self.num_class_images + self.num_train_images
        nc = self.num_class_images
        ni = self.num_train_images
        ti = nc + ni
        shared.status.job_count = p_len
        shared.status.job_no = 0
        total_instances = 0
        total_classes = 0
        if self.pbar is None:
            self.pbar = mytqdm(range(p_len),
                               desc="Caching latents..." if self.cache_latents else "Processing images...", position=0)
        else:
            self.pbar.reset(total=p_len)
            self.pbar.set_description("Caching latents..." if self.cache_latents else "Processing images...")
        self.pbar.status_index = 1
        image_cache_file = os.path.join(self.cache_dir, f"image_cache_{self.resolution}.safetensors")
        latents_cache = {}
        if os.path.exists(image_cache_file):
            print("Loading cached latents...")
            latents_cache = safetensors.torch.load_file(image_cache_file)
        for dict_idx, train_images in self.train_dict.items():
            if not train_images:
                continue
            # Separate the resolution from the index where we need it
            res = (dict_idx[0], dict_idx[1])
            # This should really be the index, because we want the bucket sampler to shuffle them all
            self.resolutions.append(dict_idx)
            # Cache with the actual res, because it's used to crop
            cache_images(train_images, res, self.pbar)
            inst_count = len(train_images)
            class_count = 0
            if dict_idx in self.class_dict:
                # Use dict index to find class images
                class_images = self.class_dict[dict_idx]
                # Use actual res here as well
                cache_images(class_images, res, self.pbar)
                class_count = len(class_images)
            total_instances += inst_count
            total_classes += class_count
            example_len = inst_count if class_count == 0 else inst_count * 2
            # Use index here, not res
            bucket_len[dict_idx] = example_len
            total_len += example_len
            bucket_str = str(bucket_idx).rjust(max_idx_chars, " ")
            inst_str = str(len(train_images)).rjust(len(str(ni)), " ")
            class_str = str(class_count).rjust(len(str(nc)), " ")
            ex_str = str(example_len).rjust(len(str(ti * 2)), " ")
            # Log both here
            self.pbar.write(
                f"Bucket {bucket_str} {dict_idx} - Instance Images: {inst_str} | Class Images: {class_str} | Max Examples/batch: {ex_str}")
            bucket_idx += 1
        try:
            if set(self.latents_cache.keys()) != set(latents_cache.keys()):
                print("Saving cache!")
                del latents_cache
                if os.path.exists(image_cache_file):
                    os.remove(image_cache_file)
                safetensors.torch.save_file(copy.deepcopy(self.latents_cache), image_cache_file)
        except:
            pass
        bucket_str = str(bucket_idx).rjust(max_idx_chars, " ")
        inst_str = str(total_instances).rjust(len(str(ni)), " ")
        class_str = str(total_classes).rjust(len(str(nc)), " ")
        tot_str = str(total_len).rjust(len(str(ti)), " ")
        self.class_count = total_classes
        self.pbar.write(
            f"Total Buckets {bucket_str} - Instance Images: {inst_str} | Class Images: {class_str} | Max Examples/batch: {tot_str}")
        self._length = total_len
        print(f"\nTotal images / batch: {self._length}, total examples: {total_len}")
        self.pbar.reset(0)

    def shuffle_buckets(self):
        sample_dict = {}
        batch_indices = []
        batch_samples = []
        keys = list(self.train_dict.keys())
        if not self.debug_dataset:
            random.shuffle(keys)
        for key in keys:
            sample_list = []
            if not self.debug_dataset:
                random.shuffle(self.train_dict[key])
            for entry in self.train_dict[key]:
                sample_list.append(entry)
                batch_indices.append(entry[0])
                batch_samples.append(entry)
                if key in self.class_dict:
                    class_entries = self.class_dict[key]
                    selection = random.choice(class_entries)
                    batch_indices.append(selection[0])
                    batch_samples.append(selection)
                    sample_list.append(selection)
            sample_dict[key] = sample_list
        self.sample_dict = sample_dict
        self.batch_indices = batch_indices
        self.batch_samples = batch_samples

    def __len__(self):
        return self._length

    def get_example(self, res):
        # Select the current bucket of image paths
        bucket = self.sample_dict[res]

        # Set start position from last iteration
        img_index = self.image_index

        # Reset image index (double-check)
        if img_index >= len(bucket):
            img_index = 0

        repeats = 0
        # Grab instance image data
        image_path, caption, is_class_image = bucket[img_index]
        image_index = self.sample_indices.index(image_path)

        img_index += 1

        # Reset image index
        if img_index >= len(bucket):
            img_index = 0
            repeats += 1

        self.image_index = img_index

        return image_index, repeats

    def __getitem__(self, index):
        example = {}
        image_path, caption, is_class_image = self.sample_cache[index]
        if not self.debug_dataset:
            image_data, input_ids, = self.load_image(image_path, caption, self.active_resolution)
            if len(self.tokenizers) > 1:
                input_ids, added_conditions = self.sdxl_cache[image_path]
                example["instance_added_cond_kwargs"] = added_conditions
        else:
            image_data = image_path
            caption, cap_tokens = self.cache_caption(image_path, caption)
            rebuilt = self.tokenizers[0].decode(cap_tokens.tolist()[0])
            input_ids = (caption, rebuilt)

        example["input_ids"] = input_ids
        example["image"] = image_data
        example["res"] = self.active_resolution
        example["is_class"] = is_class_image

        return example