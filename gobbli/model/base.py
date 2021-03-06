import logging
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from timeit import default_timer as timer
from typing import Any, Dict, Optional

import docker

from gobbli.model.context import ContainerTaskContext
from gobbli.util import (
    format_duration,
    generate_uuid,
    gobbli_dir,
    is_dir_empty,
    read_metadata,
    write_metadata,
)

LOGGER = logging.getLogger(__name__)


def model_dir() -> Path:
    return gobbli_dir() / "model"


_WEIGHTS_DIR_NAME = "weights"


class BaseModel(ABC):
    """
    Abstract base class for all models.

    Derived classes should be careful to call super().__init__(...) with the appropriate
    arguments if they override __init__() to preserve all the functionality.

    Functionality to facilitate making GPU(s) available to derived classes is available.
    """

    _METADATA_FILENAME = "gobbli-model-meta.json"

    _WEIGHTS_DIR_NAME = _WEIGHTS_DIR_NAME
    _CONTAINER_WEIGHTS_PATH = Path("/model") / _WEIGHTS_DIR_NAME

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        load_existing: bool = False,
        use_gpu: bool = False,
        nvidia_visible_devices: str = "all",
        logger: Optional[logging.Logger] = None,
        **kwargs,
    ):
        """
        Create a model.

        Args:
          data_dir: Optional path to a directory used to store model data.  If not given,
            a unique directory under GOBBLI_DIR will be created and used.
          load_existing: If True, ``data_dir`` should be a directory that was previously used
            to create a model.  Parameters will be loaded to match the original model, and
            user-specified model parameters will be ignored.  If False, the data_dir must
            be empty if it already exists.
          use_gpu: If True, use the
            nvidia-docker runtime (https://github.com/NVIDIA/nvidia-docker) to expose
            NVIDIA GPU(s) to the container.  Will cause an error if the computer you're running
            on doesn't have an NVIDIA GPU and/or doesn't have the nvidia-docker runtime installed.
          nvidia_visible_devices: Which GPUs to make available to the container; ignored if
            ``use_gpu`` is False.  If not 'all', should be a comma-separated string: ex. ``1,2``.
          logger: If passed, use this logger for logging instead of the default module-level logger.
          **kwargs: Additional model-specific parameters to be passed to the model's :meth:`init` method.
        """
        if data_dir is None:
            self._data_dir = self.model_class_dir() / generate_uuid()
        else:
            self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)

        if load_existing and self.metadata_path.exists():
            params = read_metadata(self.metadata_path)
            if len(kwargs) > 0:
                warnings.warn(
                    "User-passed params ignored due to existing model being "
                    f"loaded: {kwargs}"
                )
        else:
            if not is_dir_empty(self._data_dir):
                raise ValueError(
                    f"data_dir '{self._data_dir}' is non-empty;"
                    " it must be empty to avoid overwriting data."
                )
            params = kwargs
            write_metadata(params, self.metadata_path)

        self.use_gpu = use_gpu
        self.nvidia_visible_devices = nvidia_visible_devices

        self._logger = LOGGER
        if logger is not None:
            self._logger = logger

        self.docker_client = docker.from_env()

        self.init(params)

    @property
    def logger(self) -> logging.Logger:
        """
        Returns:
          A logger for derived models to use.
        """
        return self._logger

    @property
    def metadata_path(self) -> Path:
        """
        Returns:
         The path to the model's metadata file containing model-specific parameters.
        """
        return self.data_dir() / BaseModel._METADATA_FILENAME

    @abstractmethod
    def init(self, params: Dict[str, Any]):
        """
        Initialize a derived model using parameters specific to that model.

        Args:
          params: A dictionary where keys are parameter names and values are
            parameter values.
        """
        raise NotImplementedError

    def _base_docker_run_kwargs(self, context: ContainerTaskContext) -> Dict[str, Any]:
        """
        Establish a base set of docker run kwargs to handle GPU support, etc.
        Map directories as specified by the context.

        Returns:
            Base kwargs for any model that will be run using Docker.
        """
        kwargs = {
            "environment": {
                # Minimize the probability of containers exiting without dumping
                # buffered output
                "PYTHONUNBUFFERED": "1"
            },
            "detach": True,
            "volumes": {
                str(context.task_root_dir): {
                    "bind": str(context.container_root_dir),
                    "mode": "rw",
                },
                # Ideally we'd mount this as read-only, but some models (e.g. fastText)
                # need to write to their weights
                str(self.weights_dir): {
                    "bind": str(BaseModel._CONTAINER_WEIGHTS_PATH),
                    "mode": "rw",
                },
            },
        }  # type: Dict[str, Any]

        if self.use_gpu:
            kwargs["environment"][
                "NVIDIA_VISIBLE_DEVICES"
            ] = self.nvidia_visible_devices
            kwargs["runtime"] = "nvidia"

        return kwargs

    @property
    def _base_docker_build_kwargs(self) -> Dict[str, Any]:
        """
        Handle GPU support, etc via common args for any model Docker container.

        Returns:
            Base kwargs for any model that will be built using Docker.
        """
        kwargs = {"buildargs": {}}  # type: Dict[str, Any]

        if self.use_gpu:
            kwargs["buildargs"]["GPU"] = "1"

        return kwargs

    def data_dir(self) -> Path:
        """
        Returns:
            The main data directory unique to this instance of the model.
        """
        return self._data_dir

    @classmethod
    def model_class_dir(cls) -> Path:
        """
        Returns:
          A directory shared among all classes of the model.
        """
        return model_dir() / cls.__name__

    @property
    def weights_dir(self) -> Path:
        """
        The directory used to store initial model weights (before fine-tuning).
        These should generally be some pretrained weights made available by model
        developers.  This directory will NOT be created by default; models should
        download their weights and remove the weights directory if the download doesn't
        finish properly.

        Returns:
            The path to the weights directory.
        """
        return self.model_class_dir() / BaseModel._WEIGHTS_DIR_NAME

    def build(self):
        """
        Perform any pre-setup that needs to be done before running the model
        (building Docker images, etc).
        """
        self.logger.info("Starting build.")
        start = timer()
        self._build()
        end = timer()
        self.logger.info(f"Build finished in {format_duration(end - start)}.")

    @abstractmethod
    def _build(self):
        """
        Used for derived classes to define their implementation of the build method.
        """
        raise NotImplementedError
