# Zstandard decompression

`zstddeclib.c` is the upstream single-file decompressor generated from the verified Zstandard 1.5.7 release. The included BSD license covers its distribution. [Upstream release](https://github.com/meta/zstd/releases/tag/v1.5.7)

The upstream archive SHA-256 is `eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3`. To regenerate the source, run the following command from `build/single_file_libs` in the extracted release.

```sh
python3 combine.py -r ../../lib -x legacy/zstd_legacy.h -o zstddeclib.c zstddeclib-in.c
```
