from pathlib import Path

from setuptools import find_packages, setup

PACKAGE_NAME = "portacode"
ROOT = Path(__file__).parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

setup(
    name=PACKAGE_NAME,
    use_scm_version=True,
    setup_requires=['setuptools_scm'],
    description="Portacode CLI client and SDK",
    long_description=README,
    long_description_content_type="text/markdown",
    author="Meena Erian",
    author_email="hi@menas.pro",
    url="https://github.com/portacode/portacode",
    packages=find_packages(exclude=("tests", "server")),
    package_data={
        "portacode": [
            "static/js/**/*.js",
            "static/js/**/*.html",
            "link_capture/bin/*",
        ],
    },
    python_requires=">=3.8",
    install_requires=[
        "click>=8.0",
        "platformdirs>=3.0",
        "cryptography>=41.0",
        "websockets>=12.0",
        "pyperclip>=1.8",
        "psutil>=5.9",
        "pyte>=0.8",
        "pywinpty>=2.0; platform_system=='Windows'",
        "GitPython>=3.1.45",
        "watchdog>=3.0",
        "diff-match-patch>=20230430",
        "Pygments>=2.14.0",
        "ntplib>=0.4.0",
        "importlib_resources>=6.0",
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
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    include_package_data=True,
) 
