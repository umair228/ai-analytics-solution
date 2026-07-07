"""
Helpers for loading pre-trained NeuralProphet models.

The pre-trained ``.pkl`` artifacts were produced on a different machine, so the
pickled model carries that machine's logger ``save_dir`` (e.g. ``/Users/apple``).
During ``predict`` PyTorch-Lightning would try to write its transient logger
output there and fail with a permission error. ``sanitize_model`` rebuilds the
logger against a writable runtime directory and reconfigures the trainer.

The artifacts may also have been trained on a host with a GPU/MPS accelerator
(e.g. Apple Metal ``mps``); the pickled tensors carry that device tag, so a plain
``pickle.load`` on a CPU-only host (production is Linux, no MPS) raises
``RuntimeError: Storage device not recognized: mps``. ``load_model`` unpickles
under ``_torch_cpu_unpickle`` which pins every tensor storage to CPU.
"""
import contextlib
import os
import pickle
import tempfile

# Transient PyTorch-Lightning output (logs/checkpoints) goes here.
RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "np_runtime")
os.makedirs(RUNTIME_DIR, exist_ok=True)


@contextlib.contextmanager
def _torch_cpu_unpickle():
    """Force torch tensor storages to deserialize onto CPU while unpickling.

    Torch tensors unpickle via ``torch.storage._load_from_bytes`` -> ``torch.load``
    with no ``map_location``, so they try to restore to the device they were saved
    on. A model trained on Apple ``mps`` (or CUDA) then fails to load on a CPU-only
    box. We temporarily override that hook to pin ``map_location="cpu"`` and keep
    ``weights_only=False`` (the pickle is a trusted local artifact; torch>=2.6
    defaults ``weights_only=True``, which rejects NeuralProphet's config globals).
    """
    try:
        import io

        import torch
    except Exception:  # torch unavailable -> nothing to patch
        yield
        return

    storage = getattr(torch, "storage", None)
    orig = getattr(storage, "_load_from_bytes", None)
    if orig is None:  # torch internals moved -> fall back to a plain load
        yield
        return

    storage._load_from_bytes = lambda b: torch.load(
        io.BytesIO(b), map_location="cpu", weights_only=False
    )
    try:
        yield
    finally:
        storage._load_from_bytes = orig


def sanitize_model(model):
    """Neutralize training-machine paths baked into a pickled NeuralProphet model."""
    try:
        from neuralprophet.logger import MetricsLogger

        model.metrics_logger = MetricsLogger(save_dir=RUNTIME_DIR)
        model.metrics_logger.checkpoint_path = None
    except Exception as e:  # pragma: no cover - defensive
        print(f"[forecasting] metrics_logger reset skipped: {e}")

    trainer_config = getattr(model, "trainer_config", None)
    if isinstance(trainer_config, dict):
        trainer_config["default_root_dir"] = RUNTIME_DIR

    try:
        model.restore_trainer()
    except Exception as e:  # pragma: no cover - defensive
        print(f"[forecasting] restore_trainer skipped: {e}")
    return model


def load_model(path):
    """Load and sanitize a pickled NeuralProphet model. Raises on failure so
    callers can fall back to retraining."""
    with _torch_cpu_unpickle():
        with open(path, "rb") as f:
            model = pickle.load(f)
    return sanitize_model(model)
