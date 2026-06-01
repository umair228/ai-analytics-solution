"""
Helpers for loading pre-trained NeuralProphet models.

The pre-trained ``.pkl`` artifacts were produced on a different machine, so the
pickled model carries that machine's logger ``save_dir`` (e.g. ``/Users/apple``).
During ``predict`` PyTorch-Lightning would try to write its transient logger
output there and fail with a permission error. ``sanitize_model`` rebuilds the
logger against a writable runtime directory and reconfigures the trainer.
"""
import os
import pickle
import tempfile

# Transient PyTorch-Lightning output (logs/checkpoints) goes here.
RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "np_runtime")
os.makedirs(RUNTIME_DIR, exist_ok=True)


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
    with open(path, "rb") as f:
        model = pickle.load(f)
    return sanitize_model(model)
