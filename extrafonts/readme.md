To have your own fonts added to the pool of randomly selected fonts for your karaoke song, follow the steps below.

1. Copy your custom/extra fonts in this directory.
2. Make sure to volume mount it to the container by modifying the docker-compose.yml file.

    volumes:
      - ./extrafonts:/usr/share/fonts/extrafonts:ro # <----- Make sure this line is like this.
