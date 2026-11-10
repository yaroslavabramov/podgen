FROM postgres@sha256:778d0b486d6daa02b77434d0358ec57a1b21fd8b6d22ac2eef56a33e816928f6 AS base

FROM base AS build
RUN apk add --no-cache build-base
ADD --checksum=sha256:69f4019389af05dc1c9548deb8628e62878e6e207c03907f2b8af2016472cdaa https://codeload.github.com/pgvector/pgvector/tar.gz/refs/tags/v0.8.2 /tmp/pgvector.tar.gz
RUN mkdir /tmp/pgvector \
    && tar -xzf /tmp/pgvector.tar.gz --strip-components=1 -C /tmp/pgvector \
    && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
    && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no DESTDIR=/extension install

FROM base
COPY --from=build /extension/usr/local/ /usr/local/
