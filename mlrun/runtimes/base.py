# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import enum
import getpass
import http
import re
import traceback
import warnings
from abc import ABC, abstractmethod
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from os import environ
from typing import Callable, Dict, List, Optional, Tuple, Union

import requests.exceptions
from deprecated import deprecated
from kubernetes.client.rest import ApiException
from nuclio.build import mlrun_footer
from sqlalchemy.orm import Session

import mlrun.api.db.sqldb.session
import mlrun.api.utils.singletons.db
import mlrun.common.schemas
import mlrun.errors
import mlrun.launcher.factory
import mlrun.utils.helpers
import mlrun.utils.notifications
import mlrun.utils.regex
from mlrun.api.constants import LogSources
from mlrun.api.db.base import DBInterface
from mlrun.utils.helpers import generate_object_uri, verify_field_regex

from ..config import config
from ..datastore import store_manager
from ..db import RunDBError, get_or_set_dburl, get_run_db
from ..errors import err_to_str
from ..kfpops import mlrun_op
from ..lists import RunList
from ..model import BaseMetadata, HyperParamOptions, ImageBuilder, ModelObj, RunObject
from ..utils import (
    dict_to_json,
    dict_to_yaml,
    enrich_image_url,
    get_in,
    get_parsed_docker_registry,
    logger,
    now_date,
    update_in,
)
from .constants import PodPhases, RunStates
from .funcdoc import update_function_entry_points
from .utils import RunError, calc_hash, get_k8s

spec_fields = [
    "command",
    "args",
    "image",
    "mode",
    "build",
    "entry_points",
    "description",
    "workdir",
    "default_handler",
    "pythonpath",
    "disable_auto_mount",
    "allow_empty_resources",
    "clone_target_dir",
]


class RuntimeClassMode(enum.Enum):
    """
    Runtime class mode
    Currently there are two modes:
    1. run - the runtime class is used to run a function
    2. build - the runtime class is used to build a function

    The runtime class mode is used to determine what should be the name of the runtime class, each runtime might have a
    different name for each mode and some might not have both modes.
    """

    run = "run"
    build = "build"


class FunctionStatus(ModelObj):
    def __init__(self, state=None, build_pod=None):
        self.state = state
        self.build_pod = build_pod


class FunctionSpec(ModelObj):
    _dict_fields = spec_fields

    def __init__(
        self,
        command=None,
        args=None,
        image=None,
        mode=None,
        build=None,
        entry_points=None,
        description=None,
        workdir=None,
        default_handler=None,
        pythonpath=None,
        disable_auto_mount=False,
        clone_target_dir=None,
    ):

        self.command = command or ""
        self.image = image or ""
        self.mode = mode
        self.args = args or []
        self.rundb = None
        self.description = description or ""
        self.workdir = workdir
        self.pythonpath = pythonpath

        self._build = None
        self.build = build
        self.default_handler = default_handler
        # TODO: type verification (FunctionEntrypoint dict)
        self.entry_points = entry_points or {}
        self.disable_auto_mount = disable_auto_mount
        self.allow_empty_resources = None
        # the build.source is cloned/extracted to the specified clone_target_dir
        # if a relative path is specified, it will be enriched with a temp dir path
        self.clone_target_dir = clone_target_dir or ""

    @property
    def build(self) -> ImageBuilder:
        return self._build

    @build.setter
    def build(self, build):
        self._build = self._verify_dict(build, "build", ImageBuilder)

    def enrich_function_preemption_spec(self):
        pass

    def validate_service_account(self, allowed_service_accounts):
        pass


class BaseRuntime(ModelObj):
    kind = "base"
    _is_nested = False
    _is_remote = False
    _dict_fields = ["kind", "metadata", "spec", "status", "verbose"]

    def __init__(self, metadata=None, spec=None):
        self._metadata = None
        self.metadata = metadata
        self.kfp = None
        self._spec = None
        self.spec = spec
        self._db_conn = None
        self._secrets = None
        self._k8s = None
        self._is_built = False
        self.is_child = False
        self._status = None
        self.status = None
        self.verbose = False
        self._enriched_image = False

    def set_db_connection(self, conn):
        if not self._db_conn:
            self._db_conn = conn

    @property
    def metadata(self) -> BaseMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, metadata):
        self._metadata = self._verify_dict(metadata, "metadata", BaseMetadata)

    @property
    def spec(self) -> FunctionSpec:
        return self._spec

    @spec.setter
    def spec(self, spec):
        self._spec = self._verify_dict(spec, "spec", FunctionSpec)

    @property
    def status(self) -> FunctionStatus:
        return self._status

    @status.setter
    def status(self, status):
        self._status = self._verify_dict(status, "status", FunctionStatus)

    def set_label(self, key, value):
        self.metadata.labels[key] = str(value)
        return self

    @property
    def uri(self):
        return self._function_uri()

    def is_deployed(self):
        return True

    def _is_remote_api(self):
        db = self._get_db()
        if db and db.kind == "http":
            return True
        return False

    def _function_uri(self, tag=None, hash_key=None):
        return generate_object_uri(
            self.metadata.project,
            self.metadata.name,
            tag=tag or self.metadata.tag,
            hash_key=hash_key,
        )

    def _ensure_run_db(self):
        self.spec.rundb = self.spec.rundb or get_or_set_dburl()

    def _get_db(self):
        # TODO: remove this function and use the launcher db instead
        self._ensure_run_db()
        if not self._db_conn:
            if self.spec.rundb:
                self._db_conn = get_run_db(self.spec.rundb, secrets=self._secrets)
        return self._db_conn

    # This function is different than the auto_mount function, as it mounts to runtimes based on the configuration.
    # That's why it's named differently.
    def try_auto_mount_based_on_config(self):
        pass

    def validate_and_enrich_service_account(
        self, allowed_service_account, default_service_account
    ):
        pass

    def _fill_credentials(self):
        """
        If access key is not mask (starts with secret prefix) then fill $generate so that the API will handle filling
         of the credentials.
        We rely on the HTTPDB to send the access key session through the request header and that the API will mask
         the access key, that way we won't even store any plain access key in the function.
        """
        if self.metadata.credentials.access_key and (
            # if contains secret reference or $generate then no need to overwrite the access key
            self.metadata.credentials.access_key.startswith(
                mlrun.model.Credentials.secret_reference_prefix
            )
            or self.metadata.credentials.access_key.startswith(
                mlrun.model.Credentials.generate_access_key
            )
        ):
            return
        self.metadata.credentials.access_key = (
            mlrun.model.Credentials.generate_access_key
        )

    def run(
        self,
        runspec: Optional[
            Union["mlrun.run.RunTemplate", "mlrun.run.RunObject", dict]
        ] = None,
        handler: Optional[Union[str, Callable]] = None,
        name: Optional[str] = "",
        project: Optional[str] = "",
        params: Optional[dict] = None,
        inputs: Optional[Dict[str, str]] = None,
        out_path: Optional[str] = "",
        workdir: Optional[str] = "",
        artifact_path: Optional[str] = "",
        watch: Optional[bool] = True,
        schedule: Optional[Union[str, mlrun.common.schemas.ScheduleCronTrigger]] = None,
        hyperparams: Optional[Dict[str, list]] = None,
        hyper_param_options: Optional[HyperParamOptions] = None,
        verbose: Optional[bool] = None,
        scrape_metrics: Optional[bool] = None,
        local: Optional[bool] = False,
        local_code_path: Optional[str] = None,
        auto_build: Optional[bool] = None,
        param_file_secrets: Optional[Dict[str, str]] = None,
        notifications: Optional[List[mlrun.model.Notification]] = None,
        returns: Optional[List[Union[str, Dict[str, str]]]] = None,
    ) -> RunObject:
        """
        Run a local or remote task.

        :param runspec:        The run spec to generate the RunObject from. Can be RunTemplate | RunObject | dict.
        :param handler:        Pointer or name of a function handler.
        :param name:           Execution name.
        :param project:        Project name.
        :param params:         Input parameters (dict).
        :param inputs:         Input objects to pass to the handler. Type hints can be given so the input will be parsed
                               during runtime from `mlrun.DataItem` to the given type hint. The type hint can be given
                               in the key field of the dictionary after a colon, e.g: "<key> : <type_hint>".
        :param out_path:       Default artifact output path.
        :param artifact_path:  Default artifact output path (will replace out_path).
        :param workdir:        Default input artifacts path.
        :param watch:          Watch/follow run log.
        :param schedule:       ScheduleCronTrigger class instance or a standard crontab expression string
                               (which will be converted to the class using its `from_crontab` constructor),
                               see this link for help:
                               https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html#module-apscheduler.triggers.cron
        :param hyperparams:    Dict of param name and list of values to be enumerated e.g. {"p1": [1,2,3]}
                               the default strategy is grid search, can specify strategy (grid, list, random)
                               and other options in the hyper_param_options parameter.
        :param hyper_param_options: Dict or :py:class:`~mlrun.model.HyperParamOptions` struct of hyperparameter options.
        :param verbose:             Add verbose prints/logs.
        :param scrape_metrics:      Whether to add the `mlrun/scrape-metrics` label to this run's resources.
        :param local:               Run the function locally vs on the runtime/cluster.
        :param local_code_path:     Path of the code for local runs & debug.
        :param auto_build:          When set to True and the function require build it will be built on the first
                                    function run, use only if you don't plan on changing the build config between runs.
        :param param_file_secrets:  Dictionary of secrets to be used only for accessing the hyper-param parameter file.
                                    These secrets are only used locally and will not be stored anywhere
        :param notifications:       List of notifications to push when the run is completed
        :param returns: List of log hints - configurations for how to log the returning values from the handler's run
                        (as artifacts or results). The list's length must be equal to the amount of returning objects. A
                        log hint may be given as:

                        * A string of the key to use to log the returning value as result or as an artifact. To specify
                          The artifact type, it is possible to pass a string in the following structure:
                          "<key> : <type>". Available artifact types can be seen in `mlrun.ArtifactType`. If no
                          artifact type is specified, the object's default artifact type will be used.
                        * A dictionary of configurations to use when logging. Further info per object type and artifact
                          type can be given there. The artifact key must appear in the dictionary as "key": "the_key".

        :return: Run context object (RunObject) with run metadata, results and status
        """
        launcher = mlrun.launcher.factory.LauncherFactory.create_launcher(
            self._is_remote, local
        )
        return launcher.launch(
            runtime=self,
            task=runspec,
            handler=handler,
            name=name,
            project=project,
            params=params,
            inputs=inputs,
            out_path=out_path,
            workdir=workdir,
            artifact_path=artifact_path,
            watch=watch,
            schedule=schedule,
            hyperparams=hyperparams,
            hyper_param_options=hyper_param_options,
            verbose=verbose,
            scrape_metrics=scrape_metrics,
            local_code_path=local_code_path,
            auto_build=auto_build,
            param_file_secrets=param_file_secrets,
            notifications=notifications,
            returns=returns,
        )

    def _get_db_run(self, task: RunObject = None):
        if self._get_db() and task:
            project = task.metadata.project
            uid = task.metadata.uid
            iter = task.metadata.iteration
            try:
                return self._get_db().read_run(uid, project, iter=iter)
            except RunDBError:
                return None
        if task:
            return task.to_dict()

    def _generate_runtime_env(self, runobj: RunObject):
        runtime_env = {
            "MLRUN_EXEC_CONFIG": runobj.to_json(),
            "MLRUN_DEFAULT_PROJECT": runobj.metadata.project
            or self.metadata.project
            or config.default_project,
        }
        if runobj.spec.verbose:
            runtime_env["MLRUN_LOG_LEVEL"] = "DEBUG"
        if config.httpdb.api_url:
            runtime_env["MLRUN_DBPATH"] = config.httpdb.api_url
        if self.metadata.namespace or config.namespace:
            runtime_env["MLRUN_NAMESPACE"] = self.metadata.namespace or config.namespace
        return runtime_env

    @staticmethod
    def _handle_submit_job_http_error(error: requests.HTTPError):
        # if we receive a 400 status code, this means the request was invalid and the run wasn't created in the DB.
        # so we don't need to update the run state and we can just raise the error.
        # more status code handling can be added here if needed
        if error.response.status_code == http.HTTPStatus.BAD_REQUEST.value:
            raise mlrun.errors.MLRunBadRequestError(
                f"Bad request to mlrun api: {error.response.text}"
            )

    def _store_function(self, runspec, meta, db):
        meta.labels["kind"] = self.kind
        if "owner" not in meta.labels:
            meta.labels["owner"] = environ.get("V3IO_USERNAME") or getpass.getuser()
        if runspec.spec.output_path:
            runspec.spec.output_path = runspec.spec.output_path.replace(
                "{{run.user}}", meta.labels["owner"]
            )

        if db and self.kind != "handler":
            struct = self.to_dict()
            hash_key = db.store_function(
                struct, self.metadata.name, self.metadata.project, versioned=True
            )
            runspec.spec.function = self._function_uri(hash_key=hash_key)

    def _get_cmd_args(self, runobj: RunObject):
        extra_env = self._generate_runtime_env(runobj)
        if self.spec.pythonpath:
            extra_env["PYTHONPATH"] = self.spec.pythonpath
        args = []
        command = self.spec.command
        code = (
            self.spec.build.functionSourceCode if hasattr(self.spec, "build") else None
        )

        if runobj.spec.handler and self.spec.mode == "pass":
            raise ValueError('cannot use "pass" mode with handler')

        if code:
            extra_env["MLRUN_EXEC_CODE"] = code

        load_archive = self.spec.build.load_source_on_run and self.spec.build.source
        need_mlrun = code or load_archive or self.spec.mode != "pass"

        if need_mlrun:
            args = ["run", "--name", runobj.metadata.name, "--from-env"]
            if runobj.spec.handler:
                args += ["--handler", runobj.spec.handler]
            if self.spec.mode:
                args += ["--mode", self.spec.mode]
            if self.spec.build.origin_filename:
                args += ["--origin-file", self.spec.build.origin_filename]

            if load_archive:
                if code:
                    raise ValueError("cannot specify both code and source archive")
                args += ["--source", self.spec.build.source]
                if self.spec.workdir:
                    # set the absolute/relative path to the cloned code
                    args += ["--workdir", self.spec.workdir]

            if command:
                args += [command]

            if self.spec.args:
                if not command:
                    # * is a placeholder for the url argument in the run CLI command,
                    # where the code is passed in the `MLRUN_EXEC_CODE` meaning there is no "actual" file to execute
                    # until the run command will create that file from the env param.
                    args += ["*"]
                args = args + self.spec.args

            command = "mlrun"
        else:
            command = command.format(**runobj.spec.parameters)
            if self.spec.args:
                args = [arg.format(**runobj.spec.parameters) for arg in self.spec.args]

        extra_env = [{"name": k, "value": v} for k, v in extra_env.items()]
        return command, args, extra_env

    def _pre_run(self, runspec: RunObject, execution):
        pass

    def _post_run(self, results, execution):
        pass

    def _run(self, runobj: RunObject, execution) -> dict:
        pass

    def _run_many(self, generator, execution, runobj: RunObject) -> RunList:
        results = RunList()
        num_errors = 0
        tasks = generator.generate(runobj)
        for task in tasks:
            try:
                self.store_run(task)
                resp = self._run(task, execution)
                resp = self._update_run_state(resp, task=task)
                run_results = resp["status"].get("results", {})
                if generator.eval_stop_condition(run_results):
                    logger.info(
                        f"reached early stop condition ({generator.options.stop_condition}), stopping iterations!"
                    )
                    results.append(resp)
                    break

            except RunError as err:
                task.status.state = "error"
                error_string = err_to_str(err)
                task.status.error = error_string
                resp = self._update_run_state(task=task, err=error_string)
                num_errors += 1
                if num_errors > generator.max_errors:
                    logger.error("too many errors, stopping iterations!")
                    results.append(resp)
                    break

            results.append(resp)

        return results

    def store_run(self, runobj: RunObject):
        if self._get_db() and runobj:
            project = runobj.metadata.project
            uid = runobj.metadata.uid
            iter = runobj.metadata.iteration
            self._get_db().store_run(runobj.to_dict(), uid, project, iter=iter)

    def _store_run_dict(self, rundict: dict):
        if self._get_db() and rundict:
            project = get_in(rundict, "metadata.project", "")
            uid = get_in(rundict, "metadata.uid")
            iter = get_in(rundict, "metadata.iteration", 0)
            self._get_db().store_run(rundict, uid, project, iter=iter)

    def _update_run_state(
        self,
        resp: dict = None,
        task: RunObject = None,
        err=None,
    ) -> dict:
        """update the task state in the DB"""
        was_none = False
        if resp is None and task:
            was_none = True
            resp = self._get_db_run(task)

            if not resp:
                self.store_run(task)
                return task.to_dict()

            if task.status.status_text:
                update_in(resp, "status.status_text", task.status.status_text)

        if resp is None:
            return None

        if not isinstance(resp, dict):
            raise ValueError(f"post_run called with type {type(resp)}")

        updates = None
        last_state = get_in(resp, "status.state", "")
        kind = get_in(resp, "metadata.labels.kind", "")
        if last_state == "error" or err:
            updates = {
                "status.last_update": now_date().isoformat(),
                "status.state": "error",
            }
            update_in(resp, "status.state", "error")
            if err:
                update_in(resp, "status.error", err_to_str(err))
            err = get_in(resp, "status.error")
            if err:
                updates["status.error"] = err_to_str(err)

        elif not was_none and last_state != "completed":
            try:
                runtime_handler = mlrun.runtimes.get_runtime_handler(kind)
                updates = runtime_handler._get_run_completion_updates(resp)
            except KeyError:
                updates = BaseRuntimeHandler._get_run_completion_updates(resp)

        uid = get_in(resp, "metadata.uid")
        logger.debug(
            "Run updates",
            name=get_in(resp, "metadata.name"),
            uid=uid,
            kind=kind,
            last_state=last_state,
            updates=updates,
        )
        if self._get_db() and updates:
            project = get_in(resp, "metadata.project")
            iter = get_in(resp, "metadata.iteration", 0)
            self._get_db().update_run(updates, uid, project, iter=iter)

        return resp

    def _force_handler(self, handler):
        if not handler:
            raise RunError(f"handler must be provided for {self.kind} runtime")

    def _has_pipeline_param(self) -> bool:
        # check if the runtime has pipeline parameters
        # https://www.kubeflow.org/docs/components/pipelines/v1/sdk/parameters/
        matches = re.findall(mlrun.utils.regex.pipeline_param[0], self.to_json())
        return bool(matches)

    def full_image_path(
        self, image=None, client_version: str = None, client_python_version: str = None
    ):
        image = image or self.spec.image or ""

        image = enrich_image_url(image, client_version, client_python_version)
        if not image.startswith("."):
            return image
        registry, repository = get_parsed_docker_registry()
        if registry:
            if repository and repository not in image:
                return f"{registry}/{repository}/{image[1:]}"
            return f"{registry}/{image[1:]}"
        namespace_domain = environ.get("IGZ_NAMESPACE_DOMAIN", None)
        if namespace_domain is not None:
            return f"docker-registry.{namespace_domain}:80/{image[1:]}"
        raise RunError("local container registry is not defined")

    def as_step(
        self,
        runspec: RunObject = None,
        handler=None,
        name: str = "",
        project: str = "",
        params: dict = None,
        hyperparams=None,
        selector="",
        hyper_param_options: HyperParamOptions = None,
        inputs: dict = None,
        outputs: dict = None,
        workdir: str = "",
        artifact_path: str = "",
        image: str = "",
        labels: dict = None,
        use_db=True,
        verbose=None,
        scrape_metrics=False,
        returns: Optional[List[Union[str, Dict[str, str]]]] = None,
        auto_build: bool = False,
    ):
        """Run a local or remote task.

        :param runspec:         run template object or dict (see RunTemplate)
        :param handler:         name of the function handler
        :param name:            execution name
        :param project:         project name
        :param params:          input parameters (dict)
        :param hyperparams:     hyper parameters
        :param selector:        selection criteria for hyper params
        :param hyper_param_options:  hyper param options (selector, early stop, strategy, ..)
                            see: :py:class:`~mlrun.model.HyperParamOptions`
        :param inputs:          Input objects to pass to the handler. Type hints can be given so the input will be
                                parsed during runtime from `mlrun.DataItem` to the given type hint. The type hint can be
                                given in the key field of the dictionary after a colon, e.g: "<key> : <type_hint>".
        :param outputs:         list of outputs which can pass in the workflow
        :param artifact_path:   default artifact output path (replace out_path)
        :param workdir:         default input artifacts path
        :param image:           container image to use
        :param labels:          labels to tag the job/run with ({key:val, ..})
        :param use_db:          save function spec in the db (vs the workflow file)
        :param verbose:         add verbose prints/logs
        :param scrape_metrics:  whether to add the `mlrun/scrape-metrics` label to this run's resources
        :param returns:         List of configurations for how to log the returning values from the handler's run
                                (as artifacts or results). The list's length must be equal to the amount of returning
                                objects. A configuration may be given as:

                                * A string of the key to use to log the returning value as result or as an artifact.
                                  To specify The artifact type, it is possible to pass a string in the following
                                  structure:
                                  "<key> : <type>". Available artifact types can be seen in `mlrun.ArtifactType`. If no
                                  artifact type is specified, the object's default artifact type will be used.
                                * A dictionary of configurations to use when logging. Further info per object type and
                                  artifact type can be given there. The artifact key must appear in the dictionary as
                                  "key": "the_key".
        :param auto_build:      when set to True and the function require build it will be built on the first
                                function run, use only if you dont plan on changing the build config between runs
        :return: KubeFlow containerOp
        """

        # if the function contain KFP PipelineParams (futures) pass the full spec to the
        # ContainerOp this way KFP will substitute the params with previous step outputs
        if use_db and not self._has_pipeline_param():
            # if the same function is built as part of the pipeline we do not use the versioned function
            # rather the latest function w the same tag so we can pick up the updated image/status
            versioned = False if hasattr(self, "_build_in_pipeline") else True
            url = self.save(versioned=versioned, refresh=True)
        else:
            url = None

        if runspec is not None:
            verify_field_regex(
                "run.metadata.name", runspec.metadata.name, mlrun.utils.regex.run_name
            )

        return mlrun_op(
            name,
            project,
            function=self,
            func_url=url,
            runobj=runspec,
            handler=handler,
            params=params,
            hyperparams=hyperparams,
            selector=selector,
            hyper_param_options=hyper_param_options,
            inputs=inputs,
            returns=returns,
            outputs=outputs,
            job_image=image,
            labels=labels,
            out_path=artifact_path,
            in_path=workdir,
            verbose=verbose,
            scrape_metrics=scrape_metrics,
            auto_build=auto_build,
        )

    def with_code(self, from_file="", body=None, with_doc=True):
        """Update the function code
        This function eliminates the need to build container images every time we edit the code

        :param from_file:   blank for current notebook, or path to .py/.ipynb file
        :param body:        will use the body as the function code
        :param with_doc:    update the document of the function parameters

        :return: function object
        """
        if body and from_file:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "must provide either body or from_file argument. not both"
            )

        if (not body and not from_file) or (from_file and from_file.endswith(".ipynb")):
            from nuclio import build_file

            _, _, body = build_file(from_file, name=self.metadata.name)

        else:
            if from_file:
                with open(from_file) as fp:
                    body = fp.read()
            if self.kind == mlrun.runtimes.RuntimeKinds.serving:
                body = body + mlrun_footer.format(
                    mlrun.runtimes.serving.serving_subkind
                )

        self.spec.build.functionSourceCode = b64encode(body.encode("utf-8")).decode(
            "utf-8"
        )
        if with_doc:
            update_function_entry_points(self, body)
        return self

    def with_requirements(
        self,
        requirements: Union[str, List[str]],
        overwrite: bool = False,
        verify_base_image: bool = False,
        prepare_image_for_deploy: bool = True,
        requirements_file: str = "",
    ):
        """add package requirements from file or list to build spec.

        :param requirements:                a list of python packages
        :param requirements_file:           a local python requirements file path
        :param overwrite:                   overwrite existing requirements
        :param verify_base_image:           verify that the base image is configured
                                            (deprecated, use prepare_image_for_deploy)
        :param prepare_image_for_deploy:    prepare the image/base_image spec for deployment
        :return: function object
        """
        self.spec.build.with_requirements(requirements, requirements_file, overwrite)

        if verify_base_image or prepare_image_for_deploy:
            # TODO: remove verify_base_image in 1.6.0
            if verify_base_image:
                warnings.warn(
                    "verify_base_image is deprecated in 1.4.0 and will be removed in 1.6.0, "
                    "use prepare_image_for_deploy",
                    category=FutureWarning,
                )
            self.prepare_image_for_deploy()

        return self

    def with_commands(
        self,
        commands: List[str],
        overwrite: bool = False,
        verify_base_image: bool = False,
        prepare_image_for_deploy: bool = True,
    ):
        """add commands to build spec.

        :param commands:                    list of commands to run during build
        :param overwrite:                   overwrite existing commands
        :param verify_base_image:           verify that the base image is configured
                                            (deprecated, use prepare_image_for_deploy)
        :param prepare_image_for_deploy:    prepare the image/base_image spec for deployment

        :return: function object
        """
        self.spec.build.with_commands(commands, overwrite)

        if verify_base_image or prepare_image_for_deploy:
            # TODO: remove verify_base_image in 1.6.0
            if verify_base_image:
                warnings.warn(
                    "verify_base_image is deprecated in 1.4.0 and will be removed in 1.6.0, "
                    "use prepare_image_for_deploy",
                    category=FutureWarning,
                )

            self.prepare_image_for_deploy()
        return self

    def clean_build_params(self):
        # when using `with_requirements` we also execute `prepare_image_for_deploy` which adds the base image
        # and cleans the spec.image, so we need to restore the image back
        if self.spec.build.base_image and not self.spec.image:
            self.spec.image = self.spec.build.base_image

        self.spec.build = {}
        return self

    # TODO: remove in 1.6.0
    @deprecated(
        version="1.4.0",
        reason="'verify_base_image' will be removed in 1.6.0, use 'prepare_image_for_deploy' instead",
        category=FutureWarning,
    )
    def verify_base_image(self):
        self.prepare_image_for_deploy()

    def prepare_image_for_deploy(self):
        """
        if a function has a 'spec.image' it is considered to be deployed,
        but because we allow the user to set 'spec.image' for usability purposes,
        we need to check whether this is a built image or it requires to be built on top.
        """
        launcher = mlrun.launcher.factory.LauncherFactory.create_launcher(
            is_remote=self._is_remote
        )
        launcher.prepare_image_for_deploy(self)

    def export(self, target="", format=".yaml", secrets=None, strip=True):
        """save function spec to a local/remote path (default to./function.yaml)

        :param target:   target path/url
        :param format:   `.yaml` (default) or `.json`
        :param secrets:  optional secrets dict/object for target path (e.g. s3)
        :param strip:    strip status data

        :returns: self
        """
        if self.kind == "handler":
            raise ValueError(
                "cannot export local handler function, use "
                + "code_to_function() to serialize your function"
            )
        calc_hash(self)
        struct = self.to_dict(strip=strip)
        if format == ".yaml":
            data = dict_to_yaml(struct)
        else:
            data = dict_to_json(struct)
        stores = store_manager.set(secrets)
        target = target or "function.yaml"
        datastore, subpath = stores.get_or_create_store(target)
        datastore.put(subpath, data)
        logger.info(f"function spec saved to path: {target}")
        return self

    def save(self, tag="", versioned=False, refresh=False) -> str:
        launcher = mlrun.launcher.factory.LauncherFactory.create_launcher(
            is_remote=self._is_remote
        )
        return launcher.save_function(
            self, tag=tag, versioned=versioned, refresh=refresh
        )

    def to_dict(self, fields=None, exclude=None, strip=False):
        struct = super().to_dict(fields, exclude=exclude)
        if strip:
            if "status" in struct:
                del struct["status"]
        return struct

    def doc(self):
        print("function:", self.metadata.name)
        print(self.spec.description)
        if self.spec.default_handler:
            print("default handler:", self.spec.default_handler)
        if self.spec.entry_points:
            print("entry points:")
            for name, entry in self.spec.entry_points.items():
                print(f"  {name}: {entry.get('doc', '')}")
                params = entry.get("parameters")
                if params:
                    for p in params:
                        line = p["name"]
                        if "type" in p:
                            line += f"({p['type']})"
                        line += "  - " + p.get("doc", "")
                        if "default" in p:
                            line += f", default={p['default']}"
                        print("    " + line)


class BaseRuntimeHandler(ABC):
    # setting here to allow tests to override
    kind = "base"
    class_modes: Dict[RuntimeClassMode, str] = {}
    wait_for_deletion_interval = 10

    @staticmethod
    @abstractmethod
    def _get_object_label_selector(object_id: str) -> str:
        """
        Should return the label selector to get only resources of a specific object (with id object_id)
        """
        pass

    def _should_collect_logs(self) -> bool:
        """
        There are some runtimes which we don't collect logs for using the log collector
        :return: whether it should collect log for it
        """
        return True

    def _get_possible_mlrun_class_label_values(
        self, class_mode: Union[RuntimeClassMode, str] = None
    ) -> List[str]:
        """
        Should return the possible values of the mlrun/class label for runtime resources that are of this runtime
        handler kind
        """
        if not class_mode:
            return list(self.class_modes.values())
        class_mode = self.class_modes.get(class_mode, None)
        return [class_mode] if class_mode else []

    def list_resources(
        self,
        project: str,
        object_id: Optional[str] = None,
        label_selector: str = None,
        group_by: Optional[
            mlrun.common.schemas.ListRuntimeResourcesGroupByField
        ] = None,
    ) -> Union[
        mlrun.common.schemas.RuntimeResources,
        mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
        mlrun.common.schemas.GroupedByProjectRuntimeResourcesOutput,
    ]:
        # We currently don't support removing runtime resources in non k8s env
        if not get_k8s().is_running_inside_kubernetes_cluster():
            return {}
        namespace = get_k8s().resolve_namespace()
        label_selector = self.resolve_label_selector(project, object_id, label_selector)
        pods = self._list_pods(namespace, label_selector)
        pod_resources = self._build_pod_resources(pods)
        crd_objects = self._list_crd_objects(namespace, label_selector)
        crd_resources = self._build_crd_resources(crd_objects)
        response = self._build_list_resources_response(
            pod_resources, crd_resources, group_by
        )
        response = self._enrich_list_resources_response(
            response, namespace, label_selector, group_by
        )
        return response

    def build_output_from_runtime_resources(
        self,
        runtime_resources_list: List[mlrun.common.schemas.RuntimeResources],
        group_by: Optional[
            mlrun.common.schemas.ListRuntimeResourcesGroupByField
        ] = None,
    ):
        pod_resources = []
        crd_resources = []
        for runtime_resources in runtime_resources_list:
            pod_resources += runtime_resources.pod_resources
            crd_resources += runtime_resources.crd_resources
        response = self._build_list_resources_response(
            pod_resources, crd_resources, group_by
        )
        response = self._build_output_from_runtime_resources(
            response, runtime_resources_list, group_by
        )
        return response

    def delete_resources(
        self,
        db: DBInterface,
        db_session: Session,
        label_selector: str = None,
        force: bool = False,
        grace_period: int = None,
    ):
        if grace_period is None:
            grace_period = config.runtime_resources_deletion_grace_period
        # We currently don't support removing runtime resources in non k8s env
        if not get_k8s().is_running_inside_kubernetes_cluster():
            return
        namespace = get_k8s().resolve_namespace()
        label_selector = self.resolve_label_selector("*", label_selector=label_selector)
        crd_group, crd_version, crd_plural = self._get_crd_info()
        if crd_group and crd_version and crd_plural:
            deleted_resources = self._delete_crd_resources(
                db,
                db_session,
                namespace,
                label_selector,
                force,
                grace_period,
            )
        else:
            deleted_resources = self._delete_pod_resources(
                db,
                db_session,
                namespace,
                label_selector,
                force,
                grace_period,
            )
        self._delete_extra_resources(
            db,
            db_session,
            namespace,
            deleted_resources,
            label_selector,
            force,
            grace_period,
        )

    def delete_runtime_object_resources(
        self,
        db: DBInterface,
        db_session: Session,
        object_id: str,
        label_selector: str = None,
        force: bool = False,
        grace_period: int = None,
    ):
        if grace_period is None:
            grace_period = config.runtime_resources_deletion_grace_period
        label_selector = self._add_object_label_selector_if_needed(
            object_id, label_selector
        )
        self.delete_resources(db, db_session, label_selector, force, grace_period)

    def monitor_runs(self, db: DBInterface, db_session: Session):
        namespace = get_k8s().resolve_namespace()
        label_selector = self._get_default_label_selector()
        crd_group, crd_version, crd_plural = self._get_crd_info()
        runtime_resource_is_crd = False
        if crd_group and crd_version and crd_plural:
            runtime_resource_is_crd = True
            runtime_resources = self._list_crd_objects(namespace, label_selector)
        else:
            runtime_resources = self._list_pods(namespace, label_selector)
        project_run_uid_map = self._list_runs_for_monitoring(db, db_session)
        # project -> uid -> {"name": <runtime-resource-name>}
        run_runtime_resources_map = {}
        for runtime_resource in runtime_resources:
            project, uid, name = self._resolve_runtime_resource_run(runtime_resource)
            run_runtime_resources_map.setdefault(project, {})
            run_runtime_resources_map.get(project).update({uid: {"name": name}})
            try:
                self._monitor_runtime_resource(
                    db,
                    db_session,
                    project_run_uid_map,
                    runtime_resource,
                    runtime_resource_is_crd,
                    namespace,
                    project,
                    uid,
                    name,
                )
            except Exception as exc:
                logger.warning(
                    "Failed monitoring runtime resource. Continuing",
                    runtime_resource_name=runtime_resource["metadata"]["name"],
                    namespace=namespace,
                    exc=err_to_str(exc),
                    traceback=traceback.format_exc(),
                )
        for project, runs in project_run_uid_map.items():
            if runs:
                for run_uid, run in runs.items():
                    try:
                        if not run:
                            run = db.read_run(db_session, run_uid, project)
                        if self.kind == run.get("metadata", {}).get("labels", {}).get(
                            "kind", ""
                        ):
                            self._ensure_run_not_stuck_on_non_terminal_state(
                                db,
                                db_session,
                                project,
                                run_uid,
                                run,
                                run_runtime_resources_map,
                            )
                    except Exception as exc:
                        logger.warning(
                            "Failed ensuring run not stuck. Continuing",
                            run_uid=run_uid,
                            run=run,
                            project=project,
                            exc=err_to_str(exc),
                            traceback=traceback.format_exc(),
                        )

    def _ensure_run_not_stuck_on_non_terminal_state(
        self,
        db: DBInterface,
        db_session: Session,
        project: str,
        run_uid: str,
        run: dict = None,
        run_runtime_resources_map: dict = None,
    ):
        """
        Ensuring that a run does not become trapped in a non-terminal state as a result of not finding
        corresponding k8s resource.
        This can occur when a node is evicted or preempted, causing the resources to be removed from the resource
        listing when the final state recorded in the database is non-terminal.
        This will have a significant impact on scheduled jobs, since they will not be created until the
        previous run reaches a terminal state (because of concurrency limit)
        """
        now = now_date()
        db_run_state = run.get("status", {}).get("state")
        if not db_run_state:
            # we are setting the run state to a terminal state to avoid log spamming, this is mainly sanity as we are
            # setting state to runs when storing new runs.
            logger.info(
                "Runs monitoring found a run without state, updating to a terminal state",
                project=project,
                uid=run_uid,
                db_run_state=db_run_state,
                now=now,
            )
            run.setdefault("status", {})["state"] = RunStates.error
            run.setdefault("status", {})["last_update"] = now.isoformat()
            db.store_run(db_session, run, run_uid, project)
            return
        if db_run_state in RunStates.non_terminal_states():
            if run_runtime_resources_map and run_uid in run_runtime_resources_map.get(
                project, {}
            ):
                # if found resource there is no need to continue
                return
            last_update_str = run.get("status", {}).get("last_update")
            debounce_period = (
                config.resolve_runs_monitoring_missing_runtime_resources_debouncing_interval()
            )
            if last_update_str is None:
                logger.info(
                    "Runs monitoring found run in non-terminal state without last update time set, "
                    "updating last update time to now, to be able to evaluate next time if something changed",
                    project=project,
                    uid=run_uid,
                    db_run_state=db_run_state,
                    now=now,
                    debounce_period=debounce_period,
                )
                run.setdefault("status", {})["last_update"] = now.isoformat()
                db.store_run(db_session, run, run_uid, project)
                return

            if datetime.fromisoformat(last_update_str) > now - timedelta(
                seconds=debounce_period
            ):
                # we are setting non-terminal states to runs before the run is actually applied to k8s, meaning there is
                # a timeframe where the run exists and no runtime resources exist and it's ok, therefore we're applying
                # a debounce period before setting the state to error
                logger.warning(
                    "Monitoring did not discover a runtime resource that corresponded to a run in a "
                    "non-terminal state. but record has recently updated. Debouncing",
                    project=project,
                    uid=run_uid,
                    db_run_state=db_run_state,
                    last_update=datetime.fromisoformat(last_update_str),
                    now=now,
                    debounce_period=debounce_period,
                )
            else:
                logger.info(
                    "Updating run state", run_uid=run_uid, run_state=RunStates.error
                )
                run.setdefault("status", {})["state"] = RunStates.error
                run.setdefault("status", {})[
                    "reason"
                ] = "A runtime resource related to this run could not be found"
                run.setdefault("status", {})["last_update"] = now.isoformat()
                db.store_run(db_session, run, run_uid, project)

    def _add_object_label_selector_if_needed(
        self,
        object_id: Optional[str] = None,
        label_selector: Optional[str] = None,
    ):
        if object_id:
            object_label_selector = self._get_object_label_selector(object_id)
            if label_selector:
                label_selector = ",".join([object_label_selector, label_selector])
            else:
                label_selector = object_label_selector
        return label_selector

    @staticmethod
    def _get_main_runtime_resource_label_selector() -> str:
        """
        There are some runtimes which might have multiple k8s resources attached to a one runtime, in this case
        we don't want to pull logs from all but rather only for the "driver"/"launcher" etc
        :return: the label selector
        """
        return ""

    def _enrich_list_resources_response(
        self,
        response: Union[
            mlrun.common.schemas.RuntimeResources,
            mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
            mlrun.common.schemas.GroupedByProjectRuntimeResourcesOutput,
        ],
        namespace: str,
        label_selector: str = None,
        group_by: Optional[
            mlrun.common.schemas.ListRuntimeResourcesGroupByField
        ] = None,
    ) -> Union[
        mlrun.common.schemas.RuntimeResources,
        mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
        mlrun.common.schemas.GroupedByProjectRuntimeResourcesOutput,
    ]:
        """
        Override this to list resources other then pods or CRDs (which are handled by the base class)
        """
        return response

    def _build_output_from_runtime_resources(
        self,
        response: Union[
            mlrun.common.schemas.RuntimeResources,
            mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
            mlrun.common.schemas.GroupedByProjectRuntimeResourcesOutput,
        ],
        runtime_resources_list: List[mlrun.common.schemas.RuntimeResources],
        group_by: Optional[
            mlrun.common.schemas.ListRuntimeResourcesGroupByField
        ] = None,
    ):
        """
        Override this to add runtime resources other than pods or CRDs (which are handled by the base class) to the
        output
        """
        return response

    def _delete_extra_resources(
        self,
        db: DBInterface,
        db_session: Session,
        namespace: str,
        deleted_resources: List[Dict],
        label_selector: str = None,
        force: bool = False,
        grace_period: int = None,
    ):
        """
        Override this to handle deletion of resources other than pods or CRDs (which are handled by the base class)
        Note that this is happening after the deletion of the CRDs or the pods
        Note to add this at the beginning:
        if grace_period is None:
            grace_period = config.runtime_resources_deletion_grace_period
        """
        pass

    def _resolve_crd_object_status_info(
        self, db: DBInterface, db_session: Session, crd_object
    ) -> Tuple[bool, Optional[datetime], Optional[str]]:
        """
        Override this if the runtime has CRD resources.
        :return: Tuple with:
        1. bool determining whether the crd object is in terminal state
        2. datetime of when the crd object got into terminal state (only when the crd object in terminal state)
        3. the desired run state matching the crd object state
        """
        return False, None, None

    def _update_ui_url(
        self,
        db: DBInterface,
        db_session: Session,
        project: str,
        uid: str,
        crd_object,
        run: Dict = None,
    ):
        """
        Update the UI URL for relevant jobs.
        """
        pass

    def _resolve_pod_status_info(
        self, db: DBInterface, db_session: Session, pod: Dict
    ) -> Tuple[bool, Optional[datetime], Optional[str]]:
        """
        :return: Tuple with:
        1. bool determining whether the pod is in terminal state
        2. datetime of when the pod got into terminal state (only when the pod in terminal state)
        3. the run state matching the pod state
        """
        in_terminal_state = pod["status"]["phase"] in PodPhases.terminal_phases()
        run_state = PodPhases.pod_phase_to_run_state(pod["status"]["phase"])
        last_container_completion_time = None
        if in_terminal_state:
            for container_status in pod["status"].get("container_statuses", []):
                if container_status.get("state", {}).get("terminated"):
                    container_completion_time = container_status["state"][
                        "terminated"
                    ].get("finished_at")

                    # take latest completion time
                    if (
                        not last_container_completion_time
                        or last_container_completion_time < container_completion_time
                    ):
                        last_container_completion_time = container_completion_time

        return in_terminal_state, last_container_completion_time, run_state

    def _get_default_label_selector(
        self, class_mode: Union[RuntimeClassMode, str] = None
    ) -> str:
        """
        Override this to add a default label selector
        """
        class_values = self._get_possible_mlrun_class_label_values(class_mode)
        if not class_values:
            return ""
        if len(class_values) == 1:
            return f"mlrun/class={class_values[0]}"
        return f"mlrun/class in ({', '.join(class_values)})"

    @staticmethod
    def _get_run_completion_updates(run: dict) -> dict:
        """
        Get the required updates for the run object when it's completed and update the run object state
        Override this if the run completion is not resolved by a single execution
        """
        updates = {
            "status.last_update": now_date().isoformat(),
            "status.state": "completed",
        }
        update_in(run, "status.state", "completed")
        return updates

    @staticmethod
    def _get_crd_info() -> Tuple[str, str, str]:
        """
        Override this if the runtime has CRD resources. this should return the CRD info:
        crd group, crd version, crd plural
        """
        return "", "", ""

    @staticmethod
    def _are_resources_coupled_to_run_object() -> bool:
        """
        Some resources are tightly coupled to mlrun Run object, for example, for each Run of a Function of the job kind
        a kubernetes job is being generated, on the opposite a Function of the daskjob kind generates a dask cluster,
        and every Run is being executed using this cluster, i.e. no resources are created for the Run.
        This function should return true for runtimes in which Run are coupled to the underlying resources and therefore
        aspects of the Run (like its state) should be taken into consideration on resources deletion
        """
        return False

    @staticmethod
    def _expect_pods_without_uid() -> bool:
        return False

    def _list_pods(self, namespace: str, label_selector: str = None) -> List:
        pods = get_k8s().list_pods(namespace, selector=label_selector)
        # when we work with custom objects (list_namespaced_custom_object) it's always a dict, to be able to generalize
        # code working on runtime resource (either a custom object or a pod) we're transforming to dicts
        pods = [pod.to_dict() for pod in pods]
        return pods

    def _list_crd_objects(self, namespace: str, label_selector: str = None) -> List:
        crd_group, crd_version, crd_plural = self._get_crd_info()
        crd_objects = []
        if crd_group and crd_version and crd_plural:
            try:
                crd_objects = get_k8s().crdapi.list_namespaced_custom_object(
                    crd_group,
                    crd_version,
                    namespace,
                    crd_plural,
                    label_selector=label_selector,
                )
            except ApiException as exc:
                # ignore error if crd is not defined
                if exc.status != 404:
                    raise
            else:
                crd_objects = crd_objects["items"]
        return crd_objects

    def resolve_label_selector(
        self,
        project: str,
        object_id: Optional[str] = None,
        label_selector: Optional[str] = None,
        class_mode: Union[RuntimeClassMode, str] = None,
        with_main_runtime_resource_label_selector: bool = False,
    ) -> str:
        default_label_selector = self._get_default_label_selector(class_mode=class_mode)

        if label_selector:
            label_selector = ",".join([default_label_selector, label_selector])
        else:
            label_selector = default_label_selector

        if project and project != "*":
            label_selector = ",".join([label_selector, f"mlrun/project={project}"])

        label_selector = self._add_object_label_selector_if_needed(
            object_id, label_selector
        )

        if with_main_runtime_resource_label_selector:
            main_runtime_resource_label_selector = (
                self._get_main_runtime_resource_label_selector()
            )
            if main_runtime_resource_label_selector:
                label_selector = ",".join(
                    [label_selector, main_runtime_resource_label_selector]
                )

        return label_selector

    @staticmethod
    def resolve_object_id(
        run: dict,
    ) -> Optional[str]:
        """
        Get the object id from the run object
        Override this if the object id is not the run uid
        :param run: run object
        :return: object id
        """
        return run.get("metadata", {}).get("uid", None)

    def _wait_for_pods_deletion(
        self,
        namespace: str,
        deleted_pods: List[Dict],
        label_selector: str = None,
    ):
        deleted_pod_names = [pod_dict["metadata"]["name"] for pod_dict in deleted_pods]

        def _verify_pods_removed():
            pods = get_k8s().v1api.list_namespaced_pod(
                namespace, label_selector=label_selector
            )
            existing_pod_names = [pod.metadata.name for pod in pods.items]
            still_in_deletion_pods = set(existing_pod_names).intersection(
                deleted_pod_names
            )
            if still_in_deletion_pods:
                raise RuntimeError(
                    f"Pods are still in deletion process: {still_in_deletion_pods}"
                )

        if deleted_pod_names:
            timeout = 180
            logger.debug(
                "Waiting for pods deletion",
                timeout=timeout,
                interval=self.wait_for_deletion_interval,
            )
            mlrun.utils.retry_until_successful(
                self.wait_for_deletion_interval,
                timeout,
                logger,
                True,
                _verify_pods_removed,
            )

    def _wait_for_crds_underlying_pods_deletion(
        self,
        deleted_crds: List[Dict],
        label_selector: str = None,
    ):
        # we're using here the run identifier as the common ground to identify which pods are relevant to which CRD, so
        # if they are not coupled we are not able to wait - simply return
        # NOTE - there are surely smarter ways to do this, without depending on the run object, but as of writing this
        # none of the runtimes using CRDs are like that, so not handling it now
        if not self._are_resources_coupled_to_run_object():
            return

        def _verify_crds_underlying_pods_removed():
            project_uid_crd_map = {}
            for crd in deleted_crds:
                project, uid, _ = self._resolve_runtime_resource_run(crd)
                if not uid or not project:
                    logger.warning(
                        "Could not resolve run uid from crd. Skipping waiting for pods deletion",
                        crd=crd,
                    )
                    continue
                project_uid_crd_map.setdefault(project, {})[uid] = crd["metadata"][
                    "name"
                ]
            still_in_deletion_crds_to_pod_names = {}
            jobs_runtime_resources: mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput = self.list_resources(
                "*",
                label_selector=label_selector,
                group_by=mlrun.common.schemas.ListRuntimeResourcesGroupByField.job,
            )
            for project, project_jobs in jobs_runtime_resources.items():
                if project not in project_uid_crd_map:
                    continue
                for job_uid, job_runtime_resources in jobs_runtime_resources[
                    project
                ].items():
                    if job_uid not in project_uid_crd_map[project]:
                        continue
                    if job_runtime_resources.pod_resources:
                        still_in_deletion_crds_to_pod_names[
                            project_uid_crd_map[project][job_uid]
                        ] = [
                            pod_resource.name
                            for pod_resource in job_runtime_resources.pod_resources
                        ]
            if still_in_deletion_crds_to_pod_names:
                raise RuntimeError(
                    f"CRD underlying pods are still in deletion process: {still_in_deletion_crds_to_pod_names}"
                )

        if deleted_crds:
            timeout = 180
            logger.debug(
                "Waiting for CRDs underlying pods deletion",
                timeout=timeout,
                interval=self.wait_for_deletion_interval,
            )
            mlrun.utils.retry_until_successful(
                self.wait_for_deletion_interval,
                timeout,
                logger,
                True,
                _verify_crds_underlying_pods_removed,
            )

    def _delete_pod_resources(
        self,
        db: DBInterface,
        db_session: Session,
        namespace: str,
        label_selector: str = None,
        force: bool = False,
        grace_period: int = None,
    ) -> List[Dict]:
        if grace_period is None:
            grace_period = config.runtime_resources_deletion_grace_period
        pods = get_k8s().v1api.list_namespaced_pod(
            namespace, label_selector=label_selector
        )
        deleted_pods = []
        for pod in pods.items:
            pod_dict = pod.to_dict()

            # best effort - don't let one failure in pod deletion to cut the whole operation
            try:
                (
                    in_terminal_state,
                    last_update,
                    run_state,
                ) = self._resolve_pod_status_info(db, db_session, pod_dict)
                if not force:
                    if not in_terminal_state:
                        continue

                    # give some grace period if we have last update time
                    now = datetime.now(timezone.utc)
                    if (
                        last_update is not None
                        and last_update + timedelta(seconds=float(grace_period)) > now
                    ):
                        continue

                # if resources are tightly coupled to the run object - we want to perform some actions on the run object
                # before deleting them
                if self._are_resources_coupled_to_run_object():
                    try:
                        self._pre_deletion_runtime_resource_run_actions(
                            db, db_session, pod_dict, run_state
                        )
                    except Exception as exc:
                        # Don't prevent the deletion for failure in the pre deletion run actions
                        logger.warning(
                            "Failure in pod run pre-deletion actions. Continuing",
                            exc=repr(exc),
                            pod_name=pod.metadata.name,
                        )

                get_k8s().delete_pod(pod.metadata.name, namespace)
                deleted_pods.append(pod_dict)
            except Exception as exc:
                logger.warning(
                    f"Cleanup failed processing pod {pod.metadata.name}: {repr(exc)}. Continuing"
                )
        # TODO: don't wait for pods to be deleted, client should poll the deletion status
        self._wait_for_pods_deletion(namespace, deleted_pods, label_selector)
        return deleted_pods

    def _delete_crd_resources(
        self,
        db: DBInterface,
        db_session: Session,
        namespace: str,
        label_selector: str = None,
        force: bool = False,
        grace_period: int = None,
    ) -> List[Dict]:
        if grace_period is None:
            grace_period = config.runtime_resources_deletion_grace_period
        crd_group, crd_version, crd_plural = self._get_crd_info()
        deleted_crds = []
        try:
            crd_objects = get_k8s().crdapi.list_namespaced_custom_object(
                crd_group,
                crd_version,
                namespace,
                crd_plural,
                label_selector=label_selector,
            )
        except ApiException as exc:
            # ignore error if crd is not defined
            if exc.status != 404:
                raise
        else:
            for crd_object in crd_objects["items"]:
                # best effort - don't let one failure in pod deletion to cut the whole operation
                try:
                    (
                        in_terminal_state,
                        last_update,
                        desired_run_state,
                    ) = self._resolve_crd_object_status_info(db, db_session, crd_object)
                    if not force:
                        if not in_terminal_state:
                            continue

                        # give some grace period if we have last update time
                        now = datetime.now(timezone.utc)
                        if (
                            last_update is not None
                            and last_update + timedelta(seconds=float(grace_period))
                            > now
                        ):
                            continue

                    # if resources are tightly coupled to the run object - we want to perform some actions on the run
                    # object before deleting them
                    if self._are_resources_coupled_to_run_object():

                        try:
                            self._pre_deletion_runtime_resource_run_actions(
                                db,
                                db_session,
                                crd_object,
                                desired_run_state,
                            )
                        except Exception as exc:
                            # Don't prevent the deletion for failure in the pre deletion run actions
                            logger.warning(
                                "Failure in crd object run pre-deletion actions. Continuing",
                                exc=err_to_str(exc),
                                crd_object_name=crd_object["metadata"]["name"],
                            )

                    get_k8s().delete_crd(
                        crd_object["metadata"]["name"],
                        crd_group,
                        crd_version,
                        crd_plural,
                        namespace,
                    )
                    deleted_crds.append(crd_object)
                except Exception:
                    exc = traceback.format_exc()
                    crd_object_name = crd_object["metadata"]["name"]
                    logger.warning(
                        f"Cleanup failed processing CRD object {crd_object_name}: {err_to_str(exc)}. Continuing"
                    )
        self._wait_for_crds_underlying_pods_deletion(deleted_crds, label_selector)
        return deleted_crds

    def _pre_deletion_runtime_resource_run_actions(
        self,
        db: DBInterface,
        db_session: Session,
        runtime_resource: Dict,
        run_state: str,
    ):
        project, uid, name = self._resolve_runtime_resource_run(runtime_resource)

        # if cannot resolve related run nothing to do
        if not uid:
            if not self._expect_pods_without_uid():
                logger.warning(
                    "Could not resolve run uid from runtime resource. Skipping pre-deletion actions",
                    runtime_resource=runtime_resource,
                )
                raise ValueError("Could not resolve run uid from runtime resource")
            else:
                return

        logger.info(
            "Performing pre-deletion actions before cleaning up runtime resources",
            project=project,
            uid=uid,
        )

        self._ensure_run_state(db, db_session, project, uid, name, run_state)

        self._ensure_run_logs_collected(db, db_session, project, uid)

    def _is_runtime_resource_run_in_terminal_state(
        self,
        db: DBInterface,
        db_session: Session,
        runtime_resource: Dict,
    ) -> Tuple[bool, Optional[datetime]]:
        """
        A runtime can have different underlying resources (like pods or CRDs) - to generalize we call it runtime
        resource. This function will verify whether the Run object related to this runtime resource is in transient
        state. This is useful in order to determine whether an object can be removed. for example, a kubejob's pod
        might be in completed state, but we would like to verify that the run is completed as well to verify the logs
        were collected before we're removing the pod.

        :returns: bool determining whether the run in terminal state, and the last update time if it exists
        """
        project, uid, _ = self._resolve_runtime_resource_run(runtime_resource)

        # if no uid, assume in terminal state
        if not uid:
            return True, None

        run = db.read_run(db_session, uid, project)
        last_update = None
        last_update_str = run.get("status", {}).get("last_update")
        if last_update_str is not None:
            last_update = datetime.fromisoformat(last_update_str)

        if run.get("status", {}).get("state") not in RunStates.terminal_states():
            return False, last_update

        return True, last_update

    def _list_runs_for_monitoring(
        self, db: DBInterface, db_session: Session, states: list = None
    ):
        runs = db.list_runs(db_session, project="*", states=states)
        project_run_uid_map = {}
        run_with_missing_data = []
        duplicated_runs = []
        for run in runs:
            project = run.get("metadata", {}).get("project")
            uid = run.get("metadata", {}).get("uid")
            if not uid or not project:
                run_with_missing_data.append(run.get("metadata", {}))
                continue
            current_run = project_run_uid_map.setdefault(project, {}).get(uid)

            # sanity
            if current_run:
                duplicated_runs = {
                    "monitored_run": current_run.get(["metadata"]),
                    "duplicated_run": run.get(["metadata"]),
                }
                continue

            project_run_uid_map[project][uid] = run

        # If there are duplications or runs with missing data it probably won't be fixed
        # Monitoring is running periodically and we don't want to log on every problem we found which will spam the log
        # so we're aggregating the problems and logging only once per aggregation
        if duplicated_runs:
            logger.warning(
                "Found duplicated runs (same uid). Heuristically monitoring the first one found",
                duplicated_runs=duplicated_runs,
            )

        if run_with_missing_data:
            logger.warning(
                "Found runs with missing data. They will not be monitored",
                run_with_missing_data=run_with_missing_data,
            )

        return project_run_uid_map

    def _monitor_runtime_resource(
        self,
        db: DBInterface,
        db_session: Session,
        project_run_uid_map: Dict,
        runtime_resource: Dict,
        runtime_resource_is_crd: bool,
        namespace: str,
        project: str = None,
        uid: str = None,
        name: str = None,
    ):
        if not project and not uid and not name:
            project, uid, name = self._resolve_runtime_resource_run(runtime_resource)
        if not project or not uid:
            # Currently any build pod won't have UID and therefore will cause this log message to be printed which
            # spams the log
            # TODO: uncomment the log message when builder become a kind / starts having a UID
            # logger.warning(
            #     "Could not resolve run project or uid from runtime resource, can not monitor run. Continuing",
            #     project=project,
            #     uid=uid,
            #     runtime_resource_name=runtime_resource["metadata"]["name"],
            #     namespace=namespace,
            # )
            return
        run = project_run_uid_map.get(project, {}).get(uid)
        if runtime_resource_is_crd:
            (
                _,
                _,
                run_state,
            ) = self._resolve_crd_object_status_info(db, db_session, runtime_resource)
        else:
            (
                _,
                _,
                run_state,
            ) = self._resolve_pod_status_info(db, db_session, runtime_resource)
        self._update_ui_url(db, db_session, project, uid, runtime_resource, run)
        _, updated_run_state = self._ensure_run_state(
            db,
            db_session,
            project,
            uid,
            name,
            run_state,
            run,
            search_run=False,
        )
        if updated_run_state in RunStates.terminal_states():
            self._ensure_run_logs_collected(db, db_session, project, uid)

    def _build_list_resources_response(
        self,
        pod_resources: List[mlrun.common.schemas.RuntimeResource] = None,
        crd_resources: List[mlrun.common.schemas.RuntimeResource] = None,
        group_by: Optional[
            mlrun.common.schemas.ListRuntimeResourcesGroupByField
        ] = None,
    ) -> Union[
        mlrun.common.schemas.RuntimeResources,
        mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
        mlrun.common.schemas.GroupedByProjectRuntimeResourcesOutput,
    ]:
        if crd_resources is None:
            crd_resources = []
        if pod_resources is None:
            pod_resources = []

        if group_by is None:
            return mlrun.common.schemas.RuntimeResources(
                crd_resources=crd_resources, pod_resources=pod_resources
            )
        else:
            if group_by == mlrun.common.schemas.ListRuntimeResourcesGroupByField.job:
                return self._build_grouped_by_job_list_resources_response(
                    pod_resources, crd_resources
                )
            elif (
                group_by
                == mlrun.common.schemas.ListRuntimeResourcesGroupByField.project
            ):
                return self._build_grouped_by_project_list_resources_response(
                    pod_resources, crd_resources
                )
            else:
                raise NotImplementedError(
                    f"Provided group by field is not supported. group_by={group_by}"
                )

    def _build_grouped_by_project_list_resources_response(
        self,
        pod_resources: List[mlrun.common.schemas.RuntimeResource] = None,
        crd_resources: List[mlrun.common.schemas.RuntimeResource] = None,
    ) -> mlrun.common.schemas.GroupedByProjectRuntimeResourcesOutput:
        resources = {}
        for pod_resource in pod_resources:
            self._add_resource_to_grouped_by_project_resources_response(
                resources, "pod_resources", pod_resource
            )
        for crd_resource in crd_resources:
            self._add_resource_to_grouped_by_project_resources_response(
                resources, "crd_resources", crd_resource
            )
        return resources

    def _build_grouped_by_job_list_resources_response(
        self,
        pod_resources: List[mlrun.common.schemas.RuntimeResource] = None,
        crd_resources: List[mlrun.common.schemas.RuntimeResource] = None,
    ) -> mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput:
        resources = {}
        for pod_resource in pod_resources:
            self._add_resource_to_grouped_by_job_resources_response(
                resources, "pod_resources", pod_resource
            )
        for crd_resource in crd_resources:
            self._add_resource_to_grouped_by_job_resources_response(
                resources, "crd_resources", crd_resource
            )
        return resources

    def _add_resource_to_grouped_by_project_resources_response(
        self,
        resources: mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
        resource_field_name: str,
        resource: mlrun.common.schemas.RuntimeResource,
    ):
        if "mlrun/class" in resource.labels:
            project = resource.labels.get("mlrun/project", "")
            mlrun_class = resource.labels["mlrun/class"]
            kind = self._resolve_kind_from_class(mlrun_class)
            self._add_resource_to_grouped_by_field_resources_response(
                project, kind, resources, resource_field_name, resource
            )

    def _add_resource_to_grouped_by_job_resources_response(
        self,
        resources: mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
        resource_field_name: str,
        resource: mlrun.common.schemas.RuntimeResource,
    ):
        if "mlrun/uid" in resource.labels:
            project = resource.labels.get("mlrun/project", config.default_project)
            uid = resource.labels["mlrun/uid"]
            self._add_resource_to_grouped_by_field_resources_response(
                project, uid, resources, resource_field_name, resource
            )

    @staticmethod
    def _add_resource_to_grouped_by_field_resources_response(
        first_field_value: str,
        second_field_value: str,
        resources: mlrun.common.schemas.GroupedByJobRuntimeResourcesOutput,
        resource_field_name: str,
        resource: mlrun.common.schemas.RuntimeResource,
    ):
        if first_field_value not in resources:
            resources[first_field_value] = {}
        if second_field_value not in resources[first_field_value]:
            resources[first_field_value][
                second_field_value
            ] = mlrun.common.schemas.RuntimeResources(
                pod_resources=[], crd_resources=[]
            )
        if not getattr(
            resources[first_field_value][second_field_value], resource_field_name
        ):
            setattr(
                resources[first_field_value][second_field_value],
                resource_field_name,
                [],
            )
        getattr(
            resources[first_field_value][second_field_value], resource_field_name
        ).append(resource)

    @staticmethod
    def _resolve_kind_from_class(mlrun_class: str) -> str:
        class_to_kind_map = {}
        for kind in mlrun.runtimes.RuntimeKinds.runtime_with_handlers():
            runtime_handler = mlrun.runtimes.get_runtime_handler(kind)
            class_values = runtime_handler._get_possible_mlrun_class_label_values()
            for value in class_values:
                class_to_kind_map[value] = kind
        return class_to_kind_map[mlrun_class]

    @staticmethod
    def _get_run_label_selector(project: str, run_uid: str):
        return f"mlrun/project={project},mlrun/uid={run_uid}"

    @staticmethod
    def _ensure_run_logs_collected(
        db: DBInterface, db_session: Session, project: str, uid: str
    ):
        # import here to avoid circular imports
        import mlrun.api.crud as crud

        log_file_exists, _ = crud.Logs().log_file_exists_for_run_uid(project, uid)
        if not log_file_exists:
            # this stays for now for backwards compatibility in case we would not use the log collector but rather
            # the legacy method to pull logs
            logs_from_k8s = crud.Logs()._get_logs_legacy_method(
                db_session, project, uid, source=LogSources.K8S
            )
            if logs_from_k8s:
                logger.info("Storing run logs", project=project, uid=uid)
                crud.Logs().store_log(logs_from_k8s, project, uid, append=False)

    @staticmethod
    def _ensure_run_state(
        db: DBInterface,
        db_session: Session,
        project: str,
        uid: str,
        name: str,
        run_state: str,
        run: Dict = None,
        search_run: bool = True,
    ) -> Tuple[bool, str]:
        if run is None:
            run = {}
        if search_run:
            try:
                run = db.read_run(db_session, uid, project)
            except mlrun.errors.MLRunNotFoundError:
                run = {}
        if not run:
            logger.warning(
                "Run not found. A new run will be created",
                project=project,
                uid=uid,
                desired_run_state=run_state,
                search_run=search_run,
            )
            run = {"metadata": {"project": project, "name": name, "uid": uid}}
        db_run_state = run.get("status", {}).get("state")
        if db_run_state:
            if db_run_state == run_state:
                return False, run_state
            # if the current run state is terminal and different than the desired - log
            if db_run_state in RunStates.terminal_states():

                # This can happen when the SDK running in the user's Run updates the Run's state to terminal, but
                # before it exits, when the runtime resource is still running, the API monitoring (here) is executed
                if run_state not in RunStates.terminal_states():
                    now = datetime.now(timezone.utc)
                    last_update_str = run.get("status", {}).get("last_update")
                    if last_update_str is not None:
                        last_update = datetime.fromisoformat(last_update_str)
                        debounce_period = config.runs_monitoring_interval
                        if last_update > now - timedelta(
                            seconds=float(debounce_period)
                        ):
                            logger.warning(
                                "Monitoring found non-terminal state on runtime resource but record has recently "
                                "updated to terminal state. Debouncing",
                                project=project,
                                uid=uid,
                                db_run_state=db_run_state,
                                run_state=run_state,
                                last_update=last_update,
                                now=now,
                                debounce_period=debounce_period,
                            )
                            return False, run_state

                logger.warning(
                    "Run record has terminal state but monitoring found different state on runtime resource. Changing",
                    project=project,
                    uid=uid,
                    db_run_state=db_run_state,
                    run_state=run_state,
                )

        logger.info("Updating run state", run_state=run_state)
        run.setdefault("status", {})["state"] = run_state
        run.setdefault("status", {})["last_update"] = now_date().isoformat()
        db.store_run(db_session, run, uid, project)

        return True, run_state

    @staticmethod
    def _resolve_runtime_resource_run(runtime_resource: Dict) -> Tuple[str, str, str]:
        project = (
            runtime_resource.get("metadata", {}).get("labels", {}).get("mlrun/project")
        )
        if not project:
            project = config.default_project
        uid = runtime_resource.get("metadata", {}).get("labels", {}).get("mlrun/uid")
        name = (
            runtime_resource.get("metadata", {})
            .get("labels", {})
            .get("mlrun/name", "no-name")
        )
        return project, uid, name

    @staticmethod
    def _build_pod_resources(pods) -> List[mlrun.common.schemas.RuntimeResource]:
        pod_resources = []
        for pod in pods:
            pod_resources.append(
                mlrun.common.schemas.RuntimeResource(
                    name=pod["metadata"]["name"],
                    labels=pod["metadata"]["labels"],
                    status=pod["status"],
                )
            )
        return pod_resources

    @staticmethod
    def _build_crd_resources(
        custom_objects,
    ) -> List[mlrun.common.schemas.RuntimeResource]:
        crd_resources = []
        for custom_object in custom_objects:
            crd_resources.append(
                mlrun.common.schemas.RuntimeResource(
                    name=custom_object["metadata"]["name"],
                    labels=custom_object["metadata"]["labels"],
                    status=custom_object.get("status", {}),
                )
            )
        return crd_resources
