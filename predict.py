# Prediction interface for Cog ⚙️
# https://cog.run/python

import os
MODEL_CACHE = "weights"
BASE_URL = f"https://weights.replicate.delivery/default/multitalk/{MODEL_CACHE}/"
os.environ["HF_HOME"] = MODEL_CACHE
os.environ["TORCH_HOME"] = MODEL_CACHE
os.environ["HF_DATASETS_CACHE"] = MODEL_CACHE
os.environ["TRANSFORMERS_CACHE"] = MODEL_CACHE
os.environ["HUGGINGFACE_HUB_CACHE"] = MODEL_CACHE

import os
import subprocess
import time
import json
import tempfile
import logging
import sys
import warnings
import shutil
from typing import Optional
from datetime import datetime
from types import SimpleNamespace
from cog import BasePredictor, Input, Path

# Suppress warnings for cleaner output
warnings.filterwarnings('ignore')

import torch
import numpy as np
import random
import soundfile as sf
from PIL import Image

# Import MultiTalk components
import wan
from wan.configs import WAN_CONFIGS
from transformers import Wav2Vec2FeatureExtractor
from src.audio_analysis.wav2vec2 import Wav2Vec2Model
import librosa
import pyloudnorm as pyln
from einops import rearrange
from wan.utils.multitalk_utils import save_video_ffmpeg


def loudness_norm(audio_array, sr=16000, lufs=-23):
    meter = pyln.Meter(sr)
    loudness = meter.integrated_loudness(audio_array)
    if abs(loudness) > 100:
        return audio_array
    normalized_audio = pyln.normalize.loudness(audio_array, loudness, lufs)
    return normalized_audio


def extract_audio_from_video(filename, sample_rate=16000):
    """Extract audio from video file with robust error handling"""
    raw_audio_path = f"{os.path.splitext(os.path.basename(filename))[0]}.wav"
    ffmpeg_command = [
        "ffmpeg", "-y", "-i", str(filename), "-vn", "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", "2", raw_audio_path,
    ]
    subprocess.run(ffmpeg_command, check=True, capture_output=True)
    human_speech_array, sr = librosa.load(raw_audio_path, sr=sample_rate)
    human_speech_array = loudness_norm(human_speech_array, sr)
    os.remove(raw_audio_path)
    return human_speech_array


def audio_prepare_single(audio_path, sample_rate=16000):
    ext = os.path.splitext(audio_path)[1].lower()
    if ext in ['.mp4', '.mov', '.avi', '.mkv']:
        human_speech_array = extract_audio_from_video(audio_path, sample_rate)
        return human_speech_array
    else:
        human_speech_array, sr = librosa.load(audio_path, sr=sample_rate)
        human_speech_array = loudness_norm(human_speech_array, sr)
        return human_speech_array


def audio_prepare_multi(left_path, right_path, audio_type, sample_rate=16000):
    human_speech_array1 = audio_prepare_single(left_path)
    human_speech_array2 = audio_prepare_single(right_path)

    if audio_type=='para':
        new_human_speech1 = human_speech_array1
        new_human_speech2 = human_speech_array2
    elif audio_type=='add':
        new_human_speech1 = np.concatenate([human_speech_array1[: human_speech_array1.shape[0]], np.zeros(human_speech_array2.shape[0])]) 
        new_human_speech2 = np.concatenate([np.zeros(human_speech_array1.shape[0]), human_speech_array2[:human_speech_array2.shape[0]]])
    sum_human_speechs = new_human_speech1 + new_human_speech2
    return new_human_speech1, new_human_speech2, sum_human_speechs


def get_embedding(speech_array, wav2vec_feature_extractor, audio_encoder, sr=16000, device='cpu'):
    """Extract audio embeddings optimized for GPU processing"""
    audio_duration = len(speech_array) / sr
    video_length = audio_duration * 25  # Assume the video fps is 25

    # Extract audio features
    audio_feature = np.squeeze(
        wav2vec_feature_extractor(speech_array, sampling_rate=sr).input_values
    )
    audio_feature = torch.from_numpy(audio_feature).float().to(device=device)
    audio_feature = audio_feature.unsqueeze(0)

    # Generate embeddings on appropriate device
    with torch.no_grad():
        embeddings = audio_encoder(audio_feature, seq_len=int(video_length), output_hidden_states=True)

    if len(embeddings) == 0:
        print("⚠️ Failed to extract audio embedding")
        return None

    audio_emb = torch.stack(embeddings.hidden_states[1:], dim=1).squeeze(0)
    audio_emb = rearrange(audio_emb, "b s d -> s b d")

    # Keep on CPU for compatibility with downstream processing
    audio_emb = audio_emb.cpu().detach()
    return audio_emb


def download_weights(url: str, dest: str) -> None:
    start = time.time()
    print("[!] Initiating download from URL: ", url)
    print("[~] Destination path: ", dest)
    if ".tar" in dest:
        dest = os.path.dirname(dest)
    command = ["pget", "-vf" + ("x" if ".tar" in url else ""), url, dest]
    try:
        print(f"[~] Running command: {' '.join(command)}")
        subprocess.check_call(command, close_fds=False)
    except subprocess.CalledProcessError as e:
        print(
            f"[ERROR] Failed to download weights. Command '{' '.join(e.cmd)}' returned non-zero exit status {e.returncode}."
        )
        raise
    print("[+] Download completed in: ", time.time() - start, "seconds")


class Predictor(BasePredictor):
    def setup(self) -> None:
        """Load the model into memory to make running multiple predictions efficient"""
        # Create model cache directory if it doesn't exist
        os.makedirs(MODEL_CACHE, exist_ok=True)

        model_files = [
            "MeiGen-MultiTalk.tar",
            "Wan2.1-I2V-14B-480P.tar",
            "chinese-wav2vec2-base.tar"
        ]

        for model_file in model_files:
            url = BASE_URL + model_file
            filename = url.split("/")[-1]
            dest_path = os.path.join(MODEL_CACHE, filename)
            if not os.path.exists(dest_path.replace(".tar", "")):
                download_weights(url, dest_path)
                
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] %(levelname)s: %(message)s",
            handlers=[logging.StreamHandler(stream=sys.stdout)]
        )
        
        # Model paths
        self.ckpt_dir = "weights/Wan2.1-I2V-14B-480P"
        self.wav2vec_dir = "weights/chinese-wav2vec2-base"
        self.multitalk_dir = "weights/MeiGen-MultiTalk"
        
        # Initialize device for single GPU
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        print(f"Using device: {self.device}")
        
        # Load wav2vec models optimized for high VRAM
        print("Loading wav2vec models...")
        audio_device = self.device if torch.cuda.is_available() and torch.cuda.get_device_properties(0).total_memory > 40 * 1024**3 else 'cpu'
        print(f"Loading audio encoder on: {audio_device}")
        
        self.audio_encoder = Wav2Vec2Model.from_pretrained(
            self.wav2vec_dir, 
            local_files_only=True,
            attn_implementation="eager"
        ).to(audio_device)
        self.audio_encoder.feature_extractor._freeze_parameters()
        self.wav2vec_feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(
            self.wav2vec_dir, 
            local_files_only=True
        )
        self.audio_device = audio_device
        
        # Load MultiTalk pipeline
        print("Loading MultiTalk pipeline...")
        self.cfg = WAN_CONFIGS["multitalk-14B"]
        self.wan_i2v = wan.MultiTalkPipeline(
            config=self.cfg,
            checkpoint_dir=self.ckpt_dir,
            device_id=0,
            rank=0,
            t5_fsdp=False,
            dit_fsdp=False, 
            use_usp=False,
            t5_cpu=False  # Keep T5 on GPU for speed
        )
        
        # GPU optimizations for high-VRAM setup (A100/H100/H200)
        if torch.cuda.is_available():
            vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            print(f"🔍 Detected {vram_gb:.1f}GB VRAM")
            
            if vram_gb > 40:  # High VRAM setup
                print("🚀 High-VRAM detected: Enabling maximum performance optimizations")
                # Enable advanced GPU features for maximum speed
                torch.backends.cuda.enable_flash_sdp(True)
                torch.backends.cudnn.benchmark = True  # Optimize for consistent input sizes
                torch.backends.cuda.matmul.allow_tf32 = True  # Enable TF32 for faster matmul
                torch.backends.cudnn.allow_tf32 = True  # Enable TF32 for convolutions
                torch.cuda.empty_cache()  # Clear any existing memory
                print("⚡ Enabled Flash-SDP, TF32, and cuDNN benchmarking for maximum throughput")
            else:
                print("🔧 Standard GPU optimizations enabled")
                torch.backends.cuda.enable_flash_sdp(True)
                torch.cuda.empty_cache()
        
        print("✅ Model setup completed successfully!")

    def predict(
        self,
        image: Path = Input(description="Reference image containing the person(s) for video generation"),
        first_audio: Path = Input(description="First audio file for driving the conversation"),
        prompt: str = Input(
            description="Text prompt describing the desired interaction or conversation scenario",
            default="A smiling man and woman wearing headphones sit in front of microphones, appearing to host a podcast."
        ),
        second_audio: Optional[Path] = Input(
            description="Second audio file for multi-person conversation (optional)",
            default=None
        ),
        num_frames: int = Input(
            description="Number of frames to generate (automatically adjusted to nearest valid value of form 4n+1, e.g., 81, 181)",
            default=81,
            ge=25,
            le=201
        ),
        sampling_steps: int = Input(
            description="Number of sampling steps (higher = better quality, lower = faster)",
            default=40,
            ge=2,
            le=100
        ),
        seed: Optional[int] = Input(
            description="Random seed for reproducible results",
            default=None
        ),
        turbo: bool = Input(
            description="Enable turbo mode optimizations (adjusts thresholds and guidance scales for speed)",
            default=True
        )
    ) -> Path:
        """Generate a conversational video from audio and reference image"""
        
        # Auto-correct frame count to nearest valid value (4n+1 format)
        original_frames = num_frames
        if (num_frames - 1) % 4 != 0:
            # Find the nearest valid values
            n_lower = (num_frames - 1) // 4
            n_upper = n_lower + 1
            
            frames_lower = 4 * n_lower + 1
            frames_upper = 4 * n_upper + 1
            
            # Choose the closer one
            if abs(num_frames - frames_lower) <= abs(num_frames - frames_upper):
                num_frames = frames_lower
            else:
                num_frames = frames_upper
            
            # Ensure it's within bounds [25, 201]
            num_frames = max(25, min(num_frames, 201))
            
            # Final safety check and adjustment if needed
            while (num_frames - 1) % 4 != 0 and num_frames <= 201:
                num_frames += 1
            
            print(f"📐 Auto-corrected num_frames from {original_frames} to {num_frames} (required format: 4n+1)")
        
        # Validate final bounds
        if num_frames < 25 or num_frames > 201:
            raise ValueError(f"num_frames must be between 25 and 201, got {num_frames}")
        
        # Set random seed
        if seed is None:
            seed = random.randint(0, 99999999)
        
        print(f"🎬 Generating video with seed: {seed}")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            is_multi_person = second_audio is not None
            audio_save_dir = os.path.join(temp_dir, "audio_embeddings")
            os.makedirs(audio_save_dir, exist_ok=True)
            
            # Process audio and generate embeddings (following exact original pattern)
            if is_multi_person:
                print("🎤 Processing multi-person audio...")
                audio_type = "add"  # Sequential by default
                speech1, speech2, combined_speech = audio_prepare_multi(
                    str(first_audio), str(second_audio), audio_type
                )
                
                # Generate embeddings on optimal device
                embedding1 = get_embedding(speech1, self.wav2vec_feature_extractor, self.audio_encoder, device=self.audio_device)
                embedding2 = get_embedding(speech2, self.wav2vec_feature_extractor, self.audio_encoder, device=self.audio_device)
                
                # Save embeddings and audio
                emb1_path = os.path.join(audio_save_dir, '1.pt')
                emb2_path = os.path.join(audio_save_dir, '2.pt')
                sum_audio_path = os.path.join(audio_save_dir, 'sum.wav')
                
                torch.save(embedding1, emb1_path)
                torch.save(embedding2, emb2_path)
                sf.write(sum_audio_path, combined_speech, 16000)
                
                # Create input data (exact format from original)
                input_data = {
                    "prompt": prompt,
                    "cond_image": str(image),
                    "audio_type": audio_type,
                    "cond_audio": {
                        "person1": emb1_path,
                        "person2": emb2_path
                    },
                    "video_audio": sum_audio_path
                }
            else:
                print("🎤 Processing single-person audio...")
                speech = audio_prepare_single(str(first_audio))
                embedding = get_embedding(speech, self.wav2vec_feature_extractor, self.audio_encoder, device=self.audio_device)
                
                # Save embedding and audio
                emb_path = os.path.join(audio_save_dir, '1.pt')
                sum_audio_path = os.path.join(audio_save_dir, 'sum.wav')
                
                torch.save(embedding, emb_path)
                sf.write(sum_audio_path, speech, 16000)
                
                # Create input data (exact format from original)
                input_data = {
                    "prompt": prompt,
                    "cond_image": str(image),
                    "cond_audio": {
                        "person1": emb_path
                    },
                    "video_audio": sum_audio_path
                }
            
            print("🎬 Generating video...")
            
            # Configure generation parameters based on turbo mode and VRAM availability
            high_vram = torch.cuda.is_available() and torch.cuda.get_device_properties(0).total_memory > 40 * 1024**3
            
            if turbo:
                teacache_thresh = 0.8
                text_guide_scale = 3.0
                audio_guide_scale = 3.0
                shift = 5.0
                offload_model = False  # Never offload in turbo mode
                print(f"🚀 TURBO MODE: {sampling_steps} steps, thresh={teacache_thresh}")
            else:
                teacache_thresh = 0.3
                text_guide_scale = 5.0
                audio_guide_scale = 4.0
                shift = 7.0
                offload_model = not high_vram  # Don't offload with high VRAM for maximum speed
                print(f"🎬 QUALITY MODE: {sampling_steps} steps{', keeping models in GPU' if high_vram else ''}")
            
            # Configure optimizations using SimpleNamespace (matching original)
            extra_args = SimpleNamespace(
                use_teacache=True,
                teacache_thresh=teacache_thresh,
                use_apg=False,
                size='multitalk-480'
            )
            
            # Generate video using loaded pipeline (exact parameters from original)
            video = self.wan_i2v.generate(
                input_data,
                size_buckget="multitalk-480",
                motion_frame=25,
                frame_num=num_frames,
                shift=shift,
                sampling_steps=sampling_steps,
                text_guide_scale=text_guide_scale,
                audio_guide_scale=audio_guide_scale,
                seed=seed,
                offload_model=offload_model,
                max_frames_num=num_frames,
                extra_args=extra_args
            )
            
            # Save video (following original save pattern)
            output_name = f"multitalk_{abs(hash(prompt + str(seed))) % 10000}"
            print(f"💾 Saving video...")
            save_video_ffmpeg(video, output_name, [input_data['video_audio']])
            
            # Find and return generated video
            output_file = f"{output_name}.mp4"
            if not os.path.exists(output_file):
                # Look for any mp4 files with our output name
                for file in os.listdir("."):
                    if output_name in file and file.endswith('.mp4'):
                        output_file = file
                        break
                
                if not os.path.exists(output_file):
                    raise RuntimeError(f"Video generation failed - output file not found")
            
            # Copy to permanent location for return
            final_output = f"/tmp/final_{output_name}.mp4"
            shutil.copy2(output_file, final_output)
            
            # Cleanup GPU memory for optimal performance in subsequent runs
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                
            print(f"✅ Video generation completed: {final_output}")
            return Path(final_output)
