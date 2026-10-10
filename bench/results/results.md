| Scenario | Wall time | vs no cache | Bazel exit | Actions | Remote cache hits | AC hits / misses | Results dropped | Uploaded | Downloaded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 no cache | 88.7 s | 1.00x | 0 | 435 | 0 | - | - | - | - |
| 2 empty cache (upload) | 85.3 s | 0.96x | 0 | 435 | 0 | 0 / 379 | 0 | 36.6 MB | 0.0 MB |
| 3 warm cache | 6.4 s | 0.07x | 0 | 435 | 379 | 379 / 0 | 0 | 0.0 MB | 13.6 MB |
| 4 one file changed | 9.3 s | 0.10x | 0 | 435 | 378 | 378 / 1 | 0 | 0.6 MB | 13.6 MB |
| 5 after eviction | 69.9 s | 0.79x | 0 | 435 | 77 | 77 / 302 | 1 | 21.8 MB | 6.4 MB |
| 6 after eviction, unchecked | 77.6 s | 0.87x | 0 | 435 | 77 | 78 / 301 | 0 | 21.8 MB | 6.4 MB |
| 7 after blob loss | 46.7 s | 0.53x | 0 | 435 | 230 | 230 / 149 | 149 | 16.7 MB | 15.2 MB |
| 8 after blob loss, unchecked | 14.8 s | 0.17x | 1 | 150 | 97 | 143 / 0 | 0 | 2.6 MB | 1.8 MB |

Environment: bazel 8.8.1, python 3.14.4, cpus 8, kernel 6.18.33.2-microsoft-standard-WSL2, full store 36.6 MB, trimmed store 14.6 MB, blobs deleted 348
