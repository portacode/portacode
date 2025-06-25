from pathlib import Path

from setuptools import find_packages, setup

PACKAGE_NAME = "portacode"
ROOT = Path(__file__).parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    description="Portacode CLI client and SDK",
    long_description=README,
    long_description_content_type="text/markdown",
    author="Portacode Team",
    url="https://github.com/portacode/portacode-client",
    packages=find_packages(exclude=("tests",)),
    python_requires=">=3.8",
    install_requires=[
        "click>=8.0",
        "platformdirs>=3.0",
        "cryptography>=41.0",
        "websockets>=12.0",
    ],
    extras_require={
        "dev": ["black", "flake8", "pytest"],
    },
    entry_points={
        "console_scripts": [
            "portacode=portacode.cli:cli",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    include_package_data=True,
) 