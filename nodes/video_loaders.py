from pytube import YouTube
from pytube.exceptions import VideoUnavailable
import cv2
import os
from pathlib import Path
from PIL import Image, ImageOps
from typing import Tuple, Dict, List, Any, Union
import numpy as np

import torch
import subprocess

import execution_context
import folder_paths
from comfy.utils import common_upscale

from .logger import logger
from .utils import calculate_file_hash, validate_path, lazy_eval, hash_path


video_extensions = ['webm', 'mp4', 'mkv', 'gif']
force_sizes = ["Disabled", "256x?", "?x256", "256x256", "512x?", "?x512", "512x512", "?x768", "768x?"]

COMMON_REQUIRED_INPUTS = {
    "start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 10000.0, "step": 0.1}),
    "end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 10000.0, "step": 0.1}),
    "max_fps": ("INT", {"default": -1, "min": -1, "max": 30, "step": 1}),
    "force_size": (force_sizes,),
    "frame_load_cap": ("INT", {"default": 50, "min": 1, "max": 10000, "step": 1}),
}

EMPTY_VIDEO_INPUTS = {
    "width": ("INT", {"default": 512, "min": 64, "max": 8192, "step": 64}),
    "height": ("INT", {"default": 512, "min": 64, "max": 8192, "step": 64}),
    "frame_count": ("INT", {"default": 1, "min": 1, "max": 4096}),
    "fps": ("INT", {"default": 10, "min": 1, "max": 1000, "step": 1}),
}

def target_size(width, height, force_size) -> tuple[int, int]:
    if force_size != "Disabled":
        force_size = force_size.split("x")
        if force_size[0] == "?":
            width = (width*int(force_size[1]))//height
            #Limit to a multple of 8 for latent conversion
            #TODO: Consider instead cropping and centering to main aspect ratio
            width = int(width)+4 & ~7
            height = int(force_size[1])
        elif force_size[1] == "?":
            height = (height*int(force_size[0]))//width
            height = int(height)+4 & ~7
            width = int(force_size[0])
        else:
            width = int(force_size[0])
            height = int(force_size[1])
    return (width, height)


def frame_to_tensor(frame) -> torch.Tensor:
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    # convert frame to comfyui's expected format (taken from comfy's load image code)
    image = Image.fromarray(frame)
    image = ImageOps.exif_transpose(image)
    image = np.array(image, dtype=np.float32) / 255.0
    image = torch.from_numpy(image)[None,] 
    return image

def process_video_cap(
        video_cap,
        start_sec,
        end_sec,
        frame_load_cap,
        max_fps = None,
    ):
    fps = int(video_cap.get(cv2.CAP_PROP_FPS))
    width, height = int(video_cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(video_cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if not frame_load_cap or frame_load_cap <= 0:
        frame_load_cap = 999999

    if not end_sec:
        end_sec = frame_count / fps

    # Calculate the total number of frames in the specified time range
    video_sec = end_sec - start_sec
    original_frame_length = int(video_sec * fps)

    step = max(original_frame_length // frame_load_cap, 1)
    new_fps = fps // step

    if max_fps and 0 < max_fps < new_fps:
        if (step * new_fps) % new_fps != 0:
            logger.warning(f"Warning | new_fps: {new_fps}, max_fps: {max_fps}, modified step: int({step / max_fps * new_fps})")
        step = int(step / max_fps * new_fps)
        new_fps = max_fps
        

    start_frame = fps * start_sec
    end_frame = fps * end_sec

    frames_added = 0
    images = []

    curr_frame = start_frame

    logger.info(f"start_frame: {start_frame}\nend_frame: {end_frame}\nstep: {step}\n")

    while True:
        # Set the frame position
        int_curr_frame = int(curr_frame)

        video_cap.set(cv2.CAP_PROP_POS_FRAMES, int_curr_frame)

        ret, frame = video_cap.read()
        if not ret:
            break

        # Append the frame to the frames list
        image = frame_to_tensor(frame)
        images.append(image)
        frames_added += 1

        # if cap exists and we've reached it, stop processing frames
        if frame_load_cap > 0 and frames_added >= frame_load_cap:
            break
        if curr_frame >= end_frame:
            break

        curr_frame += step
    
    #Setup lambda for lazy audio capture
    #audio = lambda : get_audio(video, skip_first_frames * target_frame_time, frame_load_cap*target_frame_time)
    return (images, frames_added, new_fps, width, height)


def load_video_cv(
        video: str, 
        start_sec: float,
        end_sec: float,
        frame_load_cap: int = 50,
        output_dir = None,
        max_fps: int = -1,
        force_size = "Disabled",
        context: execution_context.ExecutionContext = None,
        **kwargs,
    ) -> Tuple[torch.Tensor, int, int, int, int]:

    video_cap = None
    try:
        video_cap = cv2.VideoCapture(video)
        if not video_cap.isOpened():
            raise ValueError(f"{video} could not be loaded with cv.")
        images, frames_added, fps, width, height = process_video_cap(video_cap, start_sec, end_sec, frame_load_cap, max_fps)
    
    finally:
        if video_cap:
            video_cap.release()
    if len(images) == 0:
        raise RuntimeError("No frames generated")
    images = torch.cat(images, dim=0)
    if force_size != "Disabled":
        new_size = target_size(width, height, force_size)

        if new_size[0] != width or new_size[1] != height:
            s = images.movedim(-1,1)
            s = common_upscale(s, new_size[0], new_size[1], "lanczos", "center")
            images = s.movedim(1,-1)
            width, height = new_size

    # TODO: raise an error maybe if no frames were loaded?

    # Setup lambda for lazy audio capture
    # audio = lambda : get_audio(video, skip_first_frames * target_frame_time,
    #                            frame_load_cap*target_frame_time)
    
    return (images, frames_added, fps, width, height,)


def is_gif(filename) -> bool:
    return str(filename).endswith("gif")


def get_audio(file, start_time=0, duration=0):
    # TODO: set ffmpeg_path
    ffmpeg_path = ""
    args = [ffmpeg_path, "-v", "error", "-i", file]
    if start_time > 0:
        args += ["-ss", str(start_time)]
    if duration > 0:
        args += ["-t", str(duration)]
    return subprocess.run(args + ["-f", "wav", "-"],
                          stdout=subprocess.PIPE, check=True).stdout


def download_youtube_video(
        youtube_url: str,
        start_sec: float,
        end_sec: float,
        frame_load_cap: int = 50,
        output_dir = None,
        force_size = "Disabled",
        max_fps = None,
        context: execution_context.ExecutionContext=None,
        **kwargs,
    ):
    if not output_dir:
        output_dir = os.path.join(folder_paths.get_output_directory(context.user_hash), "youtube")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    cap = None 

    try:
        yt = YouTube(youtube_url)
        stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()

        video_path = stream.download(output_dir)

        cap = cv2.VideoCapture(video_path)
        images, frames_added, fps, width, height = process_video_cap(cap, start_sec, end_sec, frame_load_cap, max_fps)
    
    finally:
        # Release the video capture object
        if cap:
            cap.release()
    
    if len(images) == 0:
        raise RuntimeError("No frames generated")
    images = torch.cat(images, dim=0)

    if force_size != "Disabled":
        new_size = target_size(width, height, force_size)        
        if new_size[0] != width or new_size[1] != height:
            s = images.movedim(-1,1)
            s = common_upscale(s, new_size[0], new_size[1], "lanczos", "center")
            images = s.movedim(1,-1)
            width, height = new_size

    #Setup lambda for lazy audio capture
    #audio = lambda : get_audio(video, skip_first_frames * target_frame_time, frame_load_cap*target_frame_time)
    return (images, frames_added, fps, width, height)


class YouTubeVideoLoader:
    @classmethod
    def INPUT_TYPES(s):

        inputs = {
            "required": {"youtube_url": ("STRING", {"default": "youtube/url/here"}),},
            "optional": {
                "output_dir": ("STRING", {"default": ""}),
            },
            "hidden": {
                "context": "EXECUTION_CONTEXT"
            }
        }
        inputs["required"].update(COMMON_REQUIRED_INPUTS)

        return inputs
    
    FUNCTION = "load_video"
    RETURN_TYPES = ("IMAGE", "INT", "INT", "INT", "INT",)
    RETURN_NAMES = ("images", "frame_count", "fps", "width", "height",)
    CATEGORY = "komojini/Video"
    
    def load_video(self, **kwargs):
        return download_youtube_video(**kwargs)


class UltimateVideoLoader:
    source = [
        "fileupload",
        # "filepath",
        "YouTube",
        "emptyvideo",
    ]
    
    @classmethod
    def INPUT_TYPES(cls, context: execution_context.ExecutionContext):
        input_dir = folder_paths.get_input_directory(context.user_hash)
        files = []
        for f in os.listdir(input_dir):
            if os.path.isfile(os.path.join(input_dir, f)):
                file_parts = f.split('.')
                if len(file_parts) > 1 and (file_parts[-1] in video_extensions):
                    files.append(f)
        
        inputs = {
            "required": {
                "source": (cls.source,),
                "youtube_url": ("STRING", {"default": "youtube/url/here"}),
                "video": ("STRING", {"default": "X://insert/path/here.mp4", "path_extensions": video_extensions}),
                "upload": (sorted(files),),
            },
            "hidden": {
                "context": "EXECUTION_CONTEXT",
            }
        }

        inputs["required"].update(COMMON_REQUIRED_INPUTS)
        inputs["required"].update(EMPTY_VIDEO_INPUTS)

        return inputs

    FUNCTION = "load_video"
    RETURN_TYPES = ("IMAGE", "INT", "INT", "INT", "INT",)
    RETURN_NAMES = ("images", "frame_count", "fps", "width", "height",)
    CATEGORY = "komojini/Video"

    def load_video(self, **kwargs):
        source = kwargs.get("source")
        context = kwargs.get("context")
        if source == "YouTube":
            images, frames_count, fps, width, height = download_youtube_video(**kwargs)
        elif source == "filepath":
            images, frames_count, fps, width, height = load_video_cv(**kwargs)
        elif source == "fileupload":
            kwargs['video'] = folder_paths.get_annotated_filepath(kwargs['upload'].strip("\""), context.user_hash)
            images, frames_count, fps, width, height = load_video_cv(**kwargs)
        elif source == "emptyvideo":
            frames_count = kwargs["frame_count"]
            width, height = kwargs["width"], kwargs["height"]
            fps = kwargs["fps"]
            images = torch.zeros([frames_count, height, width, 3])
        
        logger.debug(f"loaded video images.shape: {images.shape}, frames_count: {frames_count}, fpe: {fps}, widthxheight: {width}x{height}")
        return (images, frames_count, fps, width, height,)

    # @classmethod
    # def IS_CHANGED(s, upload, **kwargs):
    #     logger.debug(f"is_changed | source: {source}")

    #     source = kwargs.get("source")
    #     if source == "filepath":
    #         video = kwargs.get("video")
    #         return hash_path(video)
    #     elif source == "fileupload":
    #         image_path = folder_paths.get_annotated_filepath(upload)
    #         return calculate_file_hash(image_path)
    #     else:
    #         youtube_url = kwargs.get("youtube_url")
    #         return hash_path(youtube_url)
        
    # @classmethod
    # def VALIDATE_INPUTS(s, video, force_size, **kwargs):
    #     return validate_path(video, allow_none=True)