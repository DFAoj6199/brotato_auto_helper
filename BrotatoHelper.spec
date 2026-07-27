# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 — 土豆兄弟·一键托管工具
用法: pyinstaller BrotatoHelper.spec
输出: dist/BrotatoHelper/（文件夹，约 200-250MB，启动快）
"""

import sys, os
from pathlib import Path

# EasyOCR 模型路径
import easyocr
easyocr_path = Path(easyocr.__path__[0])

# EasyOCR 下载的模型权重
model_store = Path.home() / '.EasyOCR' / 'model'
model_files = [(str(pth), 'EasyOCR/model') for pth in model_store.glob('*.pth')]

# ── 排除 CUDA DLL（CPU 模式不需要，能省 100MB+）──
import torch
torch_path = Path(torch.__path__[0])
cuda_excludes = []
libs_dir = torch_path / 'lib'
if libs_dir.exists():
    for f in libs_dir.iterdir():
        name = f.name.lower()
        # 排除 NVIDIA/CUDA 相关 DLL，保留 CPU 库
        if any(x in name for x in ('cuda', 'cudnn', 'nccl', 'nv', 'cufft', 'curand',
                                    'cusparse', 'cusolver', 'cublas', 'nvtx', 'nvml')):
            cuda_excludes.append(name)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        (str(easyocr_path / 'model'), 'easyocr/model'),
        (str(easyocr_path / 'character'), 'easyocr/character'),
        # DBNet 模型配置文件（关键！缺少则 OCR 无法工作）
        (str(easyocr_path / 'DBNet' / 'configs'), 'easyocr/DBNet/configs'),
        *model_files,
    ],
    hiddenimports=[
        'easyocr', 'easyocr.model', 'easyocr.detection', 'easyocr.recognition',
        'cv2', 'numpy', 'PIL', 'mss', 'pynput.keyboard', 'pynput.mouse',
        'tkinter', 'torch',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'torchaudio',
        'tensorflow', 'tensorboard',
    ],
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ── 过滤 CUDA DLL ──
filtered_binaries = []
for name, path, typ in a.binaries:
    base = os.path.basename(name).lower()
    # 排除 CUDA DLL
    if any(x in base for x in ('cuda', 'cudnn', 'nccl', 'nvrtc', 'cufft', 'curand')):
        continue
    # 排除超大无用库
    if any(x in base for x in ('cusparse', 'cusolver', 'cublas', 'nvtx', 'nvml',
                                'nppc', 'nppial', 'nppicc', 'nppidei', 'nppif',
                                'nppig', 'nppim', 'nppist', 'nppisu', 'nppitc')):
        continue
    filtered_binaries.append((name, path, typ))

# ── 输出：onedir（文件夹格式），排除 CUDA DLL ──
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BrotatoHelper',
    console=False,
    debug=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
)

coll = COLLECT(
    exe,
    filtered_binaries,   # 已过滤 CUDA
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='BrotatoHelper',
)
