# Temporal noise vs gain, 2026-10-21

Temporal σ = std of (frame A − frame B)/√2 on the grey patch: the print texture and
light gradient cancel, leaving sensor noise. Spatial σ is the same patch in one frame.


## triton BayerRG12p: temporal noise vs gain (wedge white ≈ 0.5)

| point | exposure µs | gain dB | fps | wedge white | grey level | grey spatial σ (print+light) | grey temporal σ | temporal SNR |
|---|---|---|---|---|---|---|---|---|
| gain 0 dB | 332801 | 0.0 | 3.0 | 0.535 | 0.222 | 0.0124 | 0.00248 | 89 |
| gain 6 dB | 166799 | 6.0 | 3.0 | 0.534 | 0.222 | 0.0099 | 0.00254 | 87 |
| gain 12 dB | 83585 | 12.0 | 3.0 | 0.534 | 0.222 | 0.0102 | 0.00371 | 60 |
| gain 18 dB | 41897 | 18.0 | 13.9 | 0.536 | 0.222 | 0.0111 | 0.00563 | 39 |
| gain 24 dB | 20991 | 24.0 | 13.9 | 0.531 | 0.229 | 0.0086 | 0.00247 | 93 |
| gain 28.6 dB | 12357 | 28.6 | 13.9 | 0.532 | 0.230 | 0.0101 | 0.00224 | 103 |
| gain 36 dB | 5267 | 36.0 | 13.9 | 0.530 | 0.228 | 0.0100 | 0.00341 | 67 |
| gain 42 dB | 2646 | 42.0 | 13.9 | 0.541 | 0.227 | 0.0108 | 0.00445 | 51 |
| gain 42 dB, 4-frame average | 2646 | 42.0 | 13.9 | 0.546 | 0.228 | 0.0094 | 0.00210 | 109 |
