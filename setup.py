import pathlib
from setuptools import setup, find_packages

HERE = pathlib.Path(__file__).parent

README = (HERE / "README.md").read_text(encoding="utf-8")

setup(
    name="odc",
    version="1.0.0",
    description="Crawler for open data portals",
    long_description=README,
    long_description_content_type="text/markdown",
    url="https://github.com/aberenguerpas/OpenDataCrawler/",
    author="Alberto Berenguer Pastor",
    author_email="alberto.berenguer@ua.es",
    license="MIT",
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
    ],
    packages=find_packages(),
    include_package_data=True,
    install_requires=["tqdm","sodapy"],
    entry_points={
        "console_scripts": [
            "crawler=opendatacrawler.__main__:main",
        ]
    },
)