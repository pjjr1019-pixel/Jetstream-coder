"""Isolated GPU worker; accepts only one prompt on stdin and a PNG output path."""
from pathlib import Path
import json, os, sys
import torch
from diffusers import StableDiffusionXLPipeline

def main():
    request=json.load(sys.stdin);prompt=request.get('prompt')
    if not isinstance(prompt,str) or not 1<=len(prompt)<=4000:raise ValueError('Invalid image prompt')
    target=Path(sys.argv[1]);target.parent.mkdir(parents=True,exist_ok=True)
    model=Path(os.environ.get('JETT_IMAGE_MODEL',str(Path(__file__).resolve().parent/'image-model')))
    if not torch.cuda.is_available():raise RuntimeError('NVIDIA GPU unavailable')
    pipe=StableDiffusionXLPipeline.from_pretrained(str(model),torch_dtype=torch.float16,local_files_only=True,use_safetensors=True,variant="fp16")
    pipe.enable_model_cpu_offload();pipe.enable_vae_slicing()
    image=pipe(prompt=prompt,height=768,width=768,num_inference_steps=24,guidance_scale=7.5).images[0]
    tmp=target.with_suffix('.tmp.png');image.save(tmp,format='PNG');os.replace(tmp,target)
if __name__=='__main__':main()
