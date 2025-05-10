import torchaudio
import tempfile
from typing import Optional, List
import torch
import os
import ast
import sys
import librosa
from loguru import logger
from huggingface_hub import hf_hub_download, snapshot_download

from transformers import UMT5EncoderModel, AutoTokenizer

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from ace_step.pipeline_ace_step import ACEStepPipeline as AP
from ace_step.music_dcae.music_dcae_pipeline import MusicDCAE
from ace_step.ace_models.ace_step_transformer import ACEStepTransformer2DModel

import folder_paths
cache_dir = folder_paths.get_temp_directory()
models_dir = folder_paths.models_dir

torch.backends.cudnn.benchmark = False
torch.set_float32_matmul_precision('high')
torch.backends.cudnn.deterministic = True
torch.backends.cuda.matmul.allow_tf32 = True
os.environ["TOKENIZERS_PARALLELISM"] = "false"


class AudioCacher:
    def __init__(self, cache_dir: Optional[str] = None, default_format: str = "wav"):
        if cache_dir is None:
            self.cache_dir = tempfile.gettempdir()
        else:
            self.cache_dir = cache_dir
        
        if not os.path.exists(self.cache_dir):
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
            except OSError as e:
                raise  # 重新抛出异常，因为这是一个关键的初始化步骤
        self.default_format = default_format.lstrip('.') # 确保没有前导点
        self._files_to_cleanup_in_context: List[str] = [] # 用于上下文管理器

    def cache_audio_tensor(
        self,
        audio_tensor: torch.Tensor,
        sample_rate: int,
        filename_prefix: str = "cached_audio_",
        audio_format: Optional[str] = None
    ) -> str:
        
        current_format = (audio_format or self.default_format).lstrip('.')
        
        try:
            with tempfile.NamedTemporaryFile(
                prefix=filename_prefix,
                suffix=f".{current_format}",
                dir=self.cache_dir,
                delete=False 
            ) as tmp_file:
                temp_filepath = tmp_file.name
            
            torchaudio.save(temp_filepath, audio_tensor, sample_rate)
            
            self._files_to_cleanup_in_context.append(temp_filepath)
            return temp_filepath
        except Exception as e:
            
            if 'temp_filepath' in locals() and os.path.exists(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except OSError as e_clean:
                    logger.warning(f"Error cleaning temporary file {temp_filepath}: {e_clean}")
            raise RuntimeError(f"Failed to save audio: {e}") from e

    def cleanup_file(self, filepath: str) -> bool:
        """
        清理指定的缓存文件。

        Args:
            filepath (str): 要删除的文件的路径。

        Returns:
            bool: 如果文件成功删除或文件不存在，则返回 True；如果删除失败，则返回 False。
        """
        if not filepath:
            return True 
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                if filepath in self._files_to_cleanup_in_context:
                    self._files_to_cleanup_in_context.remove(filepath)
                return True
            except OSError as e:
                return False
        else:
            if filepath in self._files_to_cleanup_in_context:
                self._files_to_cleanup_in_context.remove(filepath)
            return True 

    def cleanup_all_tracked_files(self) -> None:
        """
        清理所有由当前上下文管理器实例跟踪的缓存文件。
        """
        # 迭代列表的副本，因为 cleanup_file 可能会修改列表
        for f_path in list(self._files_to_cleanup_in_context):
            self.cleanup_file(f_path)
        self._files_to_cleanup_in_context.clear() 

    def __enter__(self):
        """进入上下文管理器时调用。"""
        # 重置跟踪文件列表，以防同一个实例被多次用于 'with' 语句
        self._files_to_cleanup_in_context = []
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器时调用，负责清理。"""
        self.cleanup_all_tracked_files()
        # 返回 False 以便在发生异常时重新抛出异常
        return False


from ace_step.data_sampler import DataSampler

def sample_data(json_data):
    return (
            json_data["audio_duration"],
            json_data["prompt"],
            json_data["lyrics"],
            json_data["infer_step"],
            json_data["guidance_scale"],
            json_data["scheduler_type"],
            json_data["cfg_type"],
            json_data["omega_scale"],
            json_data["actual_seeds"][0],
            json_data["guidance_interval"],
            json_data["guidance_interval_decay"],
            json_data["min_guidance_scale"],
            json_data["use_erg_tag"],
            json_data["use_erg_lyric"],
            json_data["use_erg_diffusion"],
            ", ".join(map(str, json_data["oss_steps"])),
            json_data["guidance_scale_text"] if "guidance_scale_text" in json_data else 0.0,
            json_data["guidance_scale_lyric"] if "guidance_scale_lyric" in json_data else 0.0,
            )

data_sampler = DataSampler()

json_data = data_sampler.sample()
json_data = sample_data(json_data)

audio_duration,\
prompt, \
lyrics,\
infer_step, \
guidance_scale,\
scheduler_type, \
cfg_type, \
omega_scale, \
manual_seeds, \
guidance_interval, \
guidance_interval_decay, \
min_guidance_scale, \
use_erg_tag, \
use_erg_lyric, \
use_erg_diffusion, \
oss_steps, \
guidance_scale_text, \
guidance_scale_lyric = json_data

device = torch.device("cpu")
dtype = torch.float32
if torch.cuda.is_available():
    device = torch.device("cuda")
    dtype = torch.bfloat16
elif torch.backends.mps.is_available():
    device = torch.device("mps")
    dtype = torch.float16
    
def load_model():
    model_path = os.path.join(models_dir, "TTS", "ACE-Step-v1-3.5B")
    dcae_checkpoint_path = os.path.join(model_path, "music_dcae_f8c8")
    vocoder_checkpoint_path = os.path.join(model_path, "music_vocoder")
    ace_step_checkpoint_path = os.path.join(model_path, "ace_step_transformer")
    text_encoder_checkpoint_path = os.path.join(model_path, "umt5-base")

    import time
    start_time = time.time()
    print("Checkpoint not loaded, loading checkpoint...")
    music_dcae = MusicDCAE(dcae_checkpoint_path=dcae_checkpoint_path, vocoder_checkpoint_path=vocoder_checkpoint_path)
    ace_step = ACEStepTransformer2DModel.from_pretrained(ace_step_checkpoint_path, torch_dtype=dtype)
    umt5encoder = UMT5EncoderModel.from_pretrained(text_encoder_checkpoint_path, torch_dtype=dtype)
    text_tokenizer = AutoTokenizer.from_pretrained(text_encoder_checkpoint_path)
    load_model_cost = time.time() - start_time
    print(f"Model loaded in {load_model_cost:.2f} seconds.")

    return music_dcae, ace_step, umt5encoder, text_tokenizer


class GenerationParameters:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    { "audio_duration": ("FLOAT", {"default": audio_duration, "min": 0.0, "max": 240.0, "step": 1.0, "tooltip": "0 is a random length"}),
                      "infer_step": ("INT", {"default": infer_step, "min": 1, "max": 60, "step": 1}),
                      "guidance_scale": ("FLOAT", {"default": guidance_scale, "min": 0.0, "max": 200.0, "step": 0.1, "tooltip": "When guidance_scale_lyric > 1 and guidance_scale_text > 1, the guidance scale will not be applied."}),
                      "scheduler_type": (["euler", "heun"], {"default": scheduler_type, "tooltip": "euler is recommended. heun will take more time."}),
                      "cfg_type": (["cfg", "apg", "cfg_star"], {"default": cfg_type, "tooltip": "apg is recommended. cfg and cfg_star are almost the same."}),
                      "omega_scale": ("FLOAT", {"default": omega_scale, "min": -100.0, "max": 100.0, "step": 0.1, "tooltip": "Higher values can reduce artifacts"}),
                      "seed": ("INT", {"default":manual_seeds, "min": 0, "max": 4294967295, "step": 1}),
                      "guidance_interval": ("FLOAT", {"default": guidance_interval, "min": 0, "max": 1, "step": 0.01, "tooltip": "0.5 means only apply guidance in the middle steps"}),
                      "guidance_interval_decay": ("FLOAT", {"default": guidance_interval_decay, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Guidance scale will decay from guidance_scale to min_guidance_scale in the interval. 0.0 means no decay."}),
                      "min_guidance_scale": ("INT", {"default": min_guidance_scale, "min": 0, "max": 200, "step": 1}),
                      "use_erg_tag": ("BOOLEAN", {"default": use_erg_tag}),
                      "use_erg_lyric": ("BOOLEAN", {"default": use_erg_lyric}),
                      "use_erg_diffusion": ("BOOLEAN", {"default": use_erg_diffusion}),
                      "oss_steps": ("STRING", {"default": oss_steps}),
                      "guidance_scale_text": ("FLOAT", {"default": guidance_scale_text, "min": 0.0, "max": 10.0, "step": 0.1}),
                      "guidance_scale_lyric": ("FLOAT", {"default": guidance_scale_lyric, "min": 0.0, "max": 10.0, "step": 0.1}),
                    },
                }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("parameters",)
    FUNCTION = "generate"
    CATEGORY = "🎤MW/MW-ACE-Step"

    def generate(self, **kwargs):
        kwargs["manual_seeds"] = kwargs.pop("seed")
        return (str(kwargs),)


class MultiLinePromptACES:
    @classmethod
    def INPUT_TYPES(cls):
               
        return {
            "required": {
                "multi_line_prompt": ("STRING", {
                    "multiline": True, 
                    "default": prompt}),
                },
        }

    CATEGORY = "🎤MW/MW-ACE-Step"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "promptgen"
    
    def promptgen(self, multi_line_prompt: str):
        return (multi_line_prompt.strip(),)


class MultiLineLyrics:
    @classmethod
    def INPUT_TYPES(cls):
               
        return {
            "required": {
                "multi_line_prompt": ("STRING", {
                    "multiline": True, 
                    "default": lyrics}),
                },
        }

    CATEGORY = "🎤MW/MW-ACE-Step"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("lyrics",)
    FUNCTION = "lyricsgen"
    
    def lyricsgen(self, multi_line_prompt: str):
        return (multi_line_prompt.strip(),)

ap = None

class ACEStepGen:
    @classmethod
    def INPUT_TYPES(cls):
               
        return {
            "required": {
                "prompt": ("STRING", {"forceInput": True}),
                "lyrics": ("STRING", {"forceInput": True}),
                "parameters": ("STRING", {"forceInput": True}),
                "unload_model": ("BOOLEAN", {"default": True}),
                },
            "optional": {
                "ref_audio": ("AUDIO",),
                "ref_audio_strength": ("FLOAT", {"default": 0.5, "min": 0.01, "max": 1.0, "step": 0.01}),
                # "cpu_offload": ("BOOLEAN", {"default": True}),
                },
        }

    CATEGORY = "🎤MW/MW-ACE-Step"
    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("music",)
    FUNCTION = "acestepgen"
    
    def acestepgen(self, prompt: str, lyrics: str, parameters: str, ref_audio=None, ref_audio_strength=None, cpu_offload=False, unload_model=True):
        
        parameters = ast.literal_eval(parameters)
        global ap
        if ap is None:
            ap = AP(*load_model(), device=device, dtype=dtype, cpu_offload=cpu_offload)

        ac = AudioCacher(cache_dir=cache_dir)
        audio2audio_enable = False
        ref_audio_input = None

        if ref_audio is not None:
            ref_audio_path = ac.cache_audio_tensor(ref_audio["waveform"].squeeze(0), ref_audio["sample_rate"], filename_prefix="ref_audio_")
            audio2audio_enable = True
            ref_audio_strength = ref_audio_strength
            ref_audio_input = ref_audio_path

        audio_output = ap(
            prompt=prompt, 
            lyrics=lyrics, 
            task="audio2audio", 
            audio2audio_enable=audio2audio_enable, 
            ref_audio_strength=ref_audio_strength, 
            ref_audio_input=ref_audio_input, 
            **parameters
            )
        audio, sr = audio_output[0][0].unsqueeze(0), audio_output[0][1]

        if unload_model:
            ap.cleanup()
            ap = None
        
        return ({"waveform": audio, "sample_rate": sr},)


class ACEStepRepainting:
    @classmethod
    def INPUT_TYPES(cls):
               
        return {
            "required": {
                "src_audio": ("AUDIO",),
                "prompt": ("STRING", {"forceInput": True}),
                "lyrics": ("STRING", {"forceInput": True}),
                "parameters": ("STRING", {"forceInput": True}),
                "repaint_start": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1}),
                "repaint_end": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1}),
                "repaint_variance": ("FLOAT", {"default": 0.01, "min": 0.01, "max": 1.0, "step": 0.01}),
                "seed": ("INT", {"default":0, "min": 0, "max": 4294967295, "step": 1}),
                "unload_model": ("BOOLEAN", {"default": True}),
                # "cpu_offload": ("BOOLEAN", {"default": True}),
                },
        }

    CATEGORY = "🎤MW/MW-ACE-Step"
    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("music",)
    FUNCTION = "acesteprepainting"
    
    def acesteprepainting(self, src_audio, prompt: str, lyrics: str, parameters: str, repaint_start, repaint_end, repaint_variance, seed, unload_model=True, cpu_offload=False):
        retake_seeds = [str(seed)]
        ac = AudioCacher(cache_dir=cache_dir)
        src_audio_path = ac.cache_audio_tensor(src_audio["waveform"].squeeze(0), src_audio["sample_rate"], filename_prefix="src_audio_")
        
        audio_duration = librosa.get_duration(filename=src_audio_path)
        if repaint_end > audio_duration:
            repaint_end = audio_duration

        parameters = ast.literal_eval(parameters)
        parameters["audio_duration"] = audio_duration
        global ap
        if ap is None:
            ap = AP(*load_model(), device=device, dtype=dtype, cpu_offload=cpu_offload)

        audio_output = ap(
            prompt=prompt, 
            lyrics=lyrics, 
            task="repaint", 
            retake_seeds=retake_seeds, 
            src_audio_path=src_audio_path, 
            repaint_start=repaint_start, 
            repaint_end=repaint_end, 
            retake_variance=repaint_variance, 
            **parameters)
            
        audio, sr = audio_output[0][0].unsqueeze(0), audio_output[0][1]

        ac.cleanup_file(src_audio_path)
        if unload_model:
            ap.cleanup()
            ap = None
        
        return ({"waveform": audio, "sample_rate": sr},)


class ACEStepEdit:
    @classmethod
    def INPUT_TYPES(cls):
               
        return {
            "required": {
                "src_audio": ("AUDIO",),
                "prompt": ("STRING", {"forceInput": True}),
                "lyrics": ("STRING", {"forceInput": True}),
                "parameters": ("STRING", {"forceInput": True}),
                "edit_prompt": ("STRING", {"forceInput": True}),
                "edit_lyrics": ("STRING", {"forceInput": True}),
                "edit_n_min": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 1.0, "step": 0.01}),
                "edit_n_max": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "seed": ("INT", {"default":0, "min": 0, "max": 4294967295, "step": 1}),
                "unload_model": ("BOOLEAN", {"default": True}),
                # "cpu_offload": ("BOOLEAN", {"default": True}),
                },
        }

    CATEGORY = "🎤MW/MW-ACE-Step"
    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("music",)
    FUNCTION = "acestepedit"
    
    def acestepedit(self, src_audio, prompt: str, lyrics: str, parameters: str, edit_prompt, edit_lyrics, edit_n_min, edit_n_max, seed, unload_model=True, cpu_offload=False):
        retake_seeds = [str(seed)]
        ac = AudioCacher(cache_dir=cache_dir)
        src_audio_path = ac.cache_audio_tensor(src_audio["waveform"].squeeze(0), src_audio["sample_rate"], filename_prefix="src_audio_")
        
        audio_duration = librosa.get_duration(filename=src_audio_path)
        parameters = ast.literal_eval(parameters)
        parameters["audio_duration"] = audio_duration
        global ap
        if ap is None:
            ap = AP(*load_model(), device=device, dtype=dtype, cpu_offload=cpu_offload)

        audio_output = ap(
            prompt=prompt, 
            lyrics=lyrics, 
            task="edit", 
            retake_seeds=retake_seeds, 
            src_audio_path=src_audio_path, 
            edit_target_prompt = edit_prompt,
            edit_target_lyrics = edit_lyrics,
            edit_n_min = edit_n_min,
            edit_n_max = edit_n_max,
            **parameters)
            
        audio, sr = audio_output[0][0].unsqueeze(0), audio_output[0][1]

        ac.cleanup_file(src_audio_path)
        if unload_model:
            ap.cleanup()
            ap = None
        
        return ({"waveform": audio, "sample_rate": sr},)


class ACEStepExtend:
    @classmethod
    def INPUT_TYPES(cls):
               
        return {
            "required": {
                "src_audio": ("AUDIO",),
                "prompt": ("STRING", {"forceInput": True}),
                "lyrics": ("STRING", {"forceInput": True}),
                "parameters": ("STRING", {"forceInput": True}),
                "left_extend_length": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1}),
                "right_extend_length": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1}),
                # "repaint_variance": ("FLOAT", {"default": 0.01, "min": 0.01, "max": 1.0, "step": 0.01}),
                "seed": ("INT", {"default":0, "min": 0, "max": 4294967295, "step": 1}),
                "unload_model": ("BOOLEAN", {"default": True}),
                # "cpu_offload": ("BOOLEAN", {"default": True}),
                },
        }

    CATEGORY = "🎤MW/MW-ACE-Step"
    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("music",)
    FUNCTION = "acestepextend"
    
    def acestepextend(self, src_audio, prompt: str, lyrics: str, parameters: str, left_extend_length, right_extend_length, seed, unload_model=True, cpu_offload=False):
        retake_seeds = [str(seed)]
        ac = AudioCacher(cache_dir=cache_dir)
        src_audio_path = ac.cache_audio_tensor(src_audio["waveform"].squeeze(0), src_audio["sample_rate"], filename_prefix="src_audio_")
        
        audio_duration = librosa.get_duration(filename=src_audio_path)
        repaint_start = -left_extend_length
        repaint_end = audio_duration + right_extend_length

        parameters = ast.literal_eval(parameters)
        parameters["audio_duration"] = audio_duration
        global ap
        if ap is None:
            ap = AP(*load_model(), device=device, dtype=dtype, cpu_offload=cpu_offload)

        audio_output = ap(
            prompt=prompt, 
            lyrics=lyrics, 
            task="extend", 
            retake_seeds=retake_seeds, 
            src_audio_path=src_audio_path, 
            repaint_start=repaint_start, 
            repaint_end=repaint_end, 
            retake_variance=1.0,
            **parameters)
            
        audio, sr = audio_output[0][0].unsqueeze(0), audio_output[0][1]

        ac.cleanup_file(src_audio_path)
        if unload_model:
            ap.cleanup()
            ap = None
        
        return ({"waveform": audio, "sample_rate": sr},)


NODE_CLASS_MAPPINGS = {
    "ACEStepGen": ACEStepGen,
    "GenerationParameters": GenerationParameters,
    "MultiLinePromptACES": MultiLinePromptACES,
    "MultiLineLyrics": MultiLineLyrics,
    "ACEStepRepainting": ACEStepRepainting,
    "ACEStepEdit": ACEStepEdit,
    "ACEStepExtend": ACEStepExtend,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ACEStepGen": "ACE-Step",
    "GenerationParameters": "ACE-Step Parameters",
    "MultiLinePromptACES": "ACE-Step Prompt",
    "MultiLineLyrics": "ACE-Step Lyrics",
    "ACEStepRepainting": "ACE-Step Repainting",
    "ACEStepEdit": "ACE-Step Edit",
    "ACEStepExtend": "ACE-Step Extend",
}