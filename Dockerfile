FROM nvidia/cuda:10.1-cudnn7-runtime-ubuntu18.04 as nvidia-cuda-10-1
# prevents installation dialogs from prompting for user input
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends build-essential ca-certificates wget && \
    # the following packages are needed for opencv and tensorflow
    apt-get install -y --no-install-recommends libglib2.0 libxrender1 libxext6 libsm6 libgl1-mesa-glx libglib2.0 git && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && useradd -ms /bin/bash pv

USER pv
WORKDIR /home/pv

RUN wget --quiet https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda.sh && \
    /bin/bash ~/miniconda.sh -b -p /home/pv/conda && \
    rm ~/miniconda.sh && /home/pv/conda/bin/conda clean -tipsy && \
    /bin/bash -c ". /home/pv/conda/etc/profile.d/conda.sh && conda update -y -n base conda && \
    conda create -y -n pv python=3.6 && conda activate pv && \
    pip install --upgrade pip && conda install -y -c conda-forge uwsgi"

RUN git clone --depth 1 --progress https://github.com/Pointivo/Group-Free-3D.git
RUN /bin/bash -c "cd Group-Free-3D && conda activate pv && pip install requirements.txt"
RUN /bin/bash -c "sh Group-Free-3D/init.sh"