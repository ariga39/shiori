FROM pgvector/pgvector@sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38

# This is the local and CI database image. The base image is pinned above;
# running the inherited entrypoint as postgres avoids its root-only gosu
# handoff, so the final image does not retain gosu's inherited Go runtime.
RUN rm -f /usr/local/bin/gosu
USER postgres

# Preload vector at startup so the hnsw.ef_search GUC is registered before the
# first client session. The compose file uses this image's CMD unchanged.
CMD ["postgres", "-c", "shared_preload_libraries=vector"]
