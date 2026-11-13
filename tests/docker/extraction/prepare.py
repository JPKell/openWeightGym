"""Build the disposable test image and a compatible CUDA runtime without host installation changes."""

import argparse
from pathlib import Path
import subprocess


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--llama-source', type=Path, required=True)
    parser.add_argument('--cuda', type=Path, default=Path('/opt/cuda'))
    parser.add_argument('--cccl', type=Path, default=Path('/usr/include/cccl'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    for path in (args.llama_source, args.cuda, args.cccl):
        if not path.is_dir():
            parser.error('Directory is unavailable: ' + str(path))
    subprocess.run(['docker','build','-t','owg-extraction-dev:local','-f','tests/docker/extraction/Dockerfile','.'], cwd=root, check=True)
    subprocess.run(['docker','run','--rm','--name','owg-extraction-runtime-build',
        '-v',str(args.llama_source.resolve())+':/llama-src:ro', '-v',str(args.cuda.resolve())+':/opt/cuda:ro',
        '-v',str(args.cccl.resolve())+':/cccl:ro', '-v','owg-extraction-runtime:/runtime',
        '-e','CUDACXX=/opt/cuda/bin/nvcc', '-e','LD_LIBRARY_PATH=/opt/cuda/lib64:/runtime/build/bin',
        '--entrypoint','bash','owg-extraction-dev:local','-lc',
        'cmake -S /llama-src -B /runtime/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120 '
        '-DCMAKE_CUDA_FLAGS=-I/cccl -DGGML_NATIVE=OFF -DLLAMA_CURL=ON '
        '-DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF > /runtime/build.log 2>&1 && '
        'cmake --build /runtime/build --target llama-server -j 8 >> /runtime/build.log 2>&1'], check=True)
