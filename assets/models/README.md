# Bundled voice-activity model

`silero_vad.onnx` is the Silero VAD model distributed with
[openWakeWord](https://github.com/dscripka/openWakeWord) 0.4.

- Upstream model: https://github.com/snakers4/silero-vad
- License: MIT
- SHA-256: `a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28`

The daemon loads this graph directly with a single-threaded, arena-free
`onnxruntime` session. It deliberately avoids importing the full
`openwakeword` package, whose model and scikit-learn imports otherwise add
more than 100 MiB to the 192 MiB cgroup.
