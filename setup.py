import os
from subprocess import check_call

from setuptools import setup, find_packages
from setuptools.command.install import install


# Function to parse requirements.txt
def parse_requirements(filename):
    with open(filename) as f:
        return f.read().splitlines()


# Custom install command to recursively install submodules
class CustomInstallCommand(install):
    def run(self):
        # Install main package requirements
        requirements = parse_requirements('requirements.txt')
        if requirements:
            check_call(["pip", "install"] + requirements)

        # Install submodules
        submodules = [
            'pointnet2',
            # Add more submodules here
        ]
        for submodule in submodules:
            submodule_setup = os.path.join(submodule, 'setup.py')
            if os.path.exists(submodule_setup):
                check_call(["pip", "install", "-e", submodule])

        # Continue with the main package installation
        install.run(self)


setup(
    name='Group-Free-3D',
    version='0.1.0',
    packages=find_packages(),
    install_requires=parse_requirements('requirements.txt'),
    cmdclass={
        'install': CustomInstallCommand,
    }
)
