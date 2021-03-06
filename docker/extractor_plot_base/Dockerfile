FROM terraref/terrautils:1.5
LABEL maintainer="Chris Schnaufer <schnaufer@email.arizona.edu>"

# Install any users
RUN useradd -u 49044 extractor \
    && mkdir /home/extractor \
    && mkdir /home/extractor/sites

RUN chown -R extractor /home/extractor \
    && chgrp -R extractor /home/extractor 

# install and upgrade OS packages
RUN apt install -y imagemagick gdal-bin libgdal-dev

# install and upgrade pip and other python packages
RUN python -m pip install --no-cache-dir --upgrade pip 

RUN python -m pip install --upgrade numpy && \
    python -m pip install --upgrade dbfread && \
    python -m pip install --upgrade pyclowder && \
    python -m pip install gdal  && \
    python -m pip install --upgrade cryptography && \
    python -m pip install --upgrade --ignore-installed enum34 terrautils && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY extractor_base.py entrypoint.sh /home/extractor/

# command to run when starting docker
USER extractor
ENTRYPOINT ["/home/extractor/entrypoint.sh"]
CMD ["extractor"]

# Setup environment variables. These are passed into the container. You can change
# these to your setup. If RABBITMQ_URI is not set, it will try and use the rabbitmq
# server that is linked into the container. MAIN_SCRIPT is set to the script to be
# executed by entrypoint.sh
ENV RABBITMQ_EXCHANGE="terra" \
    RABBITMQ_VHOST="%2F" \
    RABBITMQ_QUEUE="terra.dronepipeline.extractor" \
    MAIN_SCRIPT="extractor_base.py"
