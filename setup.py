import os

from setuptools import find_packages
from setuptools import setup

here = os.path.abspath(os.path.dirname(__file__))
share_jupyterhub = os.path.join(here, "share", "jupyterhub")


def get_data_files():
    """Get data files in share/jupyterhub"""

    data_files = []
    for d, dirs, filenames in os.walk(share_jupyterhub):
        rel_d = os.path.relpath(d, here)
        data_files.append((rel_d, [os.path.join(rel_d, f) for f in filenames]))
    return data_files


setup(
    name="jupyterhub-outpostspawner",
    version="0.0.2",
    description="JupyterHub Spawner to run services on multiple remote resources.",
    url="https://github.com/kreuzert/jupyterhub-outpostspawner",
    author="Tim Kreuzer",
    author_email="t.kreuzer@fz-juelich.de",
    license="3-BSD",
    data_files=get_data_files(),
    packages=find_packages(),
    install_requires=["jupyterhub", "traitlets", "escapism", "kubernetes"],
    python_requires=">=3.9",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
)
