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
#
import json
import os
import typing
import warnings

import sqlalchemy.orm

import mlrun.api.api.endpoints.functions
import mlrun.api.api.utils
import mlrun.api.crud.runtimes.nuclio.function
import mlrun.api.utils.singletons.k8s
import mlrun.artifacts
import mlrun.common.model_monitoring as model_monitoring_constants
import mlrun.common.schemas
import mlrun.common.schemas.model_endpoints
import mlrun.config
import mlrun.datastore.store_resources
import mlrun.errors
import mlrun.feature_store
import mlrun.model_monitoring.helpers
import mlrun.utils.helpers
import mlrun.utils.model_monitoring
import mlrun.utils.v3io_clients
from mlrun.model_monitoring.stores import get_model_endpoint_store
from mlrun.utils import logger


class ModelEndpoints:
    """Provide different methods for handling model endpoints such as listing, writing and deleting"""

    def create_or_patch(
        self,
        db_session: sqlalchemy.orm.Session,
        access_key: str,
        model_endpoint: mlrun.common.schemas.ModelEndpoint,
        auth_info: mlrun.common.schemas.AuthInfo = mlrun.common.schemas.AuthInfo(),
    ) -> mlrun.common.schemas.ModelEndpoint:
        # TODO: deprecated in 1.3.0, remove in 1.5.0.
        warnings.warn(
            "This is deprecated in 1.3.0, and will be removed in 1.5.0."
            "Please use create_model_endpoint() for create or patch_model_endpoint() for update",
            FutureWarning,
        )
        """
        Either create or updates the record of a given `ModelEndpoint` object.
        Leaving here for backwards compatibility, remove in 1.5.0.

        :param db_session:             A session that manages the current dialog with the database
        :param access_key:             Access key with permission to write to KV table
        :param model_endpoint:         Model endpoint object to update
        :param auth_info:              The auth info of the request

        :return: `ModelEndpoint` object.
        """

        return self.create_model_endpoint(
            db_session=db_session, model_endpoint=model_endpoint
        )

    def create_model_endpoint(
        self,
        db_session: sqlalchemy.orm.Session,
        model_endpoint: mlrun.common.schemas.ModelEndpoint,
    ) -> mlrun.common.schemas.ModelEndpoint:
        """
        Creates model endpoint record in DB. The DB target type is defined under
        `mlrun.config.model_endpoint_monitoring.store_type` (V3IO-NOSQL by default).

        :param db_session:             A session that manages the current dialog with the database.
        :param model_endpoint:         Model endpoint object to update.

        :return: `ModelEndpoint` object.
        """

        if model_endpoint.spec.model_uri or model_endpoint.status.feature_stats:
            logger.info(
                "Getting feature metadata",
                project=model_endpoint.metadata.project,
                model=model_endpoint.spec.model,
                function=model_endpoint.spec.function_uri,
                model_uri=model_endpoint.spec.model_uri,
            )

        # If model artifact was supplied, grab model metadata from artifact
        if model_endpoint.spec.model_uri:
            logger.info(
                "Getting model object, inferring column names and collecting feature stats"
            )
            run_db = mlrun.api.api.utils.get_run_db_instance(db_session)
            model_obj: mlrun.artifacts.ModelArtifact = (
                mlrun.datastore.store_resources.get_store_resource(
                    model_endpoint.spec.model_uri, db=run_db
                )
            )

            # Get stats from model object if not found in model endpoint object
            if not model_endpoint.status.feature_stats and hasattr(
                model_obj, "feature_stats"
            ):
                model_endpoint.status.feature_stats = model_obj.spec.feature_stats
            # Get labels from model object if not found in model endpoint object
            if not model_endpoint.spec.label_names and model_obj.spec.outputs:
                model_label_names = [
                    self._clean_feature_name(f.name) for f in model_obj.spec.outputs
                ]
                model_endpoint.spec.label_names = model_label_names

            # Get algorithm from model object if not found in model endpoint object
            if not model_endpoint.spec.algorithm and model_obj.spec.algorithm:
                model_endpoint.spec.algorithm = model_obj.spec.algorithm

            # Create monitoring feature set if monitoring found in model endpoint object
            if (
                model_endpoint.spec.monitoring_mode
                == mlrun.common.model_monitoring.ModelMonitoringMode.enabled.value
            ):
                monitoring_feature_set = self.create_monitoring_feature_set(
                    model_endpoint, model_obj, db_session, run_db
                )
                # Link model endpoint object to feature set URI
                model_endpoint.status.monitoring_feature_set_uri = (
                    monitoring_feature_set.uri
                )

        # If feature_stats was either populated by model_uri or by manual input, make sure to keep the names
        # of the features. If feature_names was supplied, replace the names set in feature_stats, otherwise - make
        # sure to keep a clean version of the names
        if model_endpoint.status.feature_stats:
            logger.info("Feature stats found, cleaning feature names")
            if model_endpoint.spec.feature_names:
                # Validate that the length of feature_stats is equal to the length of feature_names and label_names
                self._validate_length_features_and_labels(model_endpoint)

                # Clean feature names in both feature_stats and feature_names
            (
                model_endpoint.status.feature_stats,
                model_endpoint.spec.feature_names,
            ) = self._adjust_feature_names_and_stats(model_endpoint=model_endpoint)

            logger.info(
                "Done preparing feature names and stats",
                feature_names=model_endpoint.spec.feature_names,
            )

        # If none of the above was supplied, feature names will be assigned on first contact with the model monitoring
        # system
        logger.info("Creating model endpoint", endpoint_id=model_endpoint.metadata.uid)

        # Write the new model endpoint
        model_endpoint_store = get_model_endpoint_store(
            project=model_endpoint.metadata.project,
        )
        model_endpoint_store.write_model_endpoint(endpoint=model_endpoint.flat_dict())

        logger.info("Model endpoint created", endpoint_id=model_endpoint.metadata.uid)

        return model_endpoint

    def create_monitoring_feature_set(
        self,
        model_endpoint: mlrun.common.schemas.ModelEndpoint,
        model_obj: mlrun.artifacts.ModelArtifact,
        db_session: sqlalchemy.orm.Session,
        run_db: mlrun.db.sqldb.SQLDB,
    ):
        """
        Create monitoring feature set with the relevant parquet target.

        :param model_endpoint:    An object representing the model endpoint.
        :param model_obj:         An object representing the deployed model.
        :param db_session:        A session that manages the current dialog with the database.
        :param run_db:            A run db instance which will be used for retrieving the feature vector in case
                                  the features are not found in the model object.

        :return:                  Feature set object for the monitoring of the current model endpoint.
        """

        # Define a new feature set
        _, serving_function_name, _, _ = mlrun.utils.helpers.parse_versioned_object_uri(
            model_endpoint.spec.function_uri
        )

        model_name = model_endpoint.spec.model.replace(":", "-")

        feature_set = mlrun.feature_store.FeatureSet(
            f"monitoring-{serving_function_name}-{model_name}",
            entities=[model_monitoring_constants.EventFieldType.ENDPOINT_ID],
            timestamp_key=model_monitoring_constants.EventFieldType.TIMESTAMP,
            description=f"Monitoring feature set for endpoint: {model_endpoint.spec.model}",
        )
        feature_set.metadata.project = model_endpoint.metadata.project

        feature_set.metadata.labels = {
            model_monitoring_constants.EventFieldType.ENDPOINT_ID: model_endpoint.metadata.uid,
            model_monitoring_constants.EventFieldType.MODEL_CLASS: model_endpoint.spec.model_class,
        }

        # Add features to the feature set according to the model object
        if model_obj.spec.inputs:
            for feature in model_obj.spec.inputs:
                feature_set.add_feature(
                    mlrun.feature_store.Feature(
                        name=feature.name, value_type=feature.value_type
                    )
                )
        # Check if features can be found within the feature vector
        elif model_obj.spec.feature_vector:
            _, name, _, tag, _ = mlrun.utils.helpers.parse_artifact_uri(
                model_obj.spec.feature_vector
            )
            fv = run_db.get_feature_vector(
                name=name, project=model_endpoint.metadata.project, tag=tag
            )
            for feature in fv.status.features:
                if feature["name"] != fv.status.label_column:
                    feature_set.add_feature(
                        mlrun.feature_store.Feature(
                            name=feature["name"], value_type=feature["value_type"]
                        )
                    )
        else:
            logger.warn(
                "Could not find any features in the model object and in the Feature Vector"
            )

        # Define parquet target for this feature set
        parquet_path = (
            self._get_monitoring_parquet_path(
                db_session=db_session, project=model_endpoint.metadata.project
            )
            + f"/key={model_endpoint.metadata.uid}"
        )

        parquet_target = mlrun.datastore.targets.ParquetTarget(
            model_monitoring_constants.FileTargetKind.PARQUET, parquet_path
        )
        driver = mlrun.datastore.targets.get_target_driver(parquet_target, feature_set)

        feature_set.set_targets(
            [mlrun.datastore.targets.ParquetTarget(path=parquet_path)],
            with_defaults=False,
        )
        driver.update_resource_status("created")

        # Save the new feature set
        feature_set._override_run_db(db_session)
        feature_set.save()
        logger.info(
            "Monitoring feature set created",
            model_endpoint=model_endpoint.spec.model,
            parquet_target=parquet_path,
        )

        return feature_set

    @staticmethod
    def _get_monitoring_parquet_path(
        db_session: sqlalchemy.orm.Session, project: str
    ) -> str:
        """Getting model monitoring parquet target for the current project. The parquet target path is based on the
        project artifact path. If project artifact path is not defined, the parquet target path will be based on MLRun
        artifact path.

        :param db_session: A session that manages the current dialog with the database. Will be used in this function
                           to get the project record from DB.
        :param project:    Project name.

        :return:           Monitoring parquet target path.
        """

        # Get the artifact path from the project record that was stored in the DB
        project_obj = mlrun.api.crud.projects.Projects().get_project(
            session=db_session, name=project
        )
        artifact_path = project_obj.spec.artifact_path
        # Generate monitoring parquet path value
        parquet_path = mlrun.mlconf.get_model_monitoring_file_target_path(
            project=project,
            kind=model_monitoring_constants.FileTargetKind.PARQUET,
            target="offline",
            artifact_path=artifact_path,
        )
        return parquet_path

    @staticmethod
    def _validate_length_features_and_labels(model_endpoint):
        """
        Validate that the length of feature_stats is equal to the length of `feature_names` and `label_names`

        :param model_endpoint:    An object representing the model endpoint.
        """

        # Getting the length of label names, feature_names and feature_stats
        len_of_label_names = (
            0
            if not model_endpoint.spec.label_names
            else len(model_endpoint.spec.label_names)
        )
        len_of_feature_names = len(model_endpoint.spec.feature_names)
        len_of_feature_stats = len(model_endpoint.status.feature_stats)

        if len_of_feature_stats != len_of_feature_names + len_of_label_names:
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"The length of model endpoint feature_stats is not equal to the "
                f"length of model endpoint feature names and labels "
                f"feature_stats({len_of_feature_stats}), "
                f"feature_names({len_of_feature_names}),"
                f"label_names({len_of_label_names}"
            )

    def _adjust_feature_names_and_stats(
        self, model_endpoint
    ) -> typing.Tuple[typing.Dict, typing.List]:
        """
        Create a clean matching version of feature names for both `feature_stats` and `feature_names`. Please note that
        label names exist only in `feature_stats` and `label_names`.

        :param model_endpoint:    An object representing the model endpoint.
        :return: A tuple of:
             [0] = Dictionary of feature stats with cleaned names
             [1] = List of cleaned feature names
        """
        clean_feature_stats = {}
        clean_feature_names = []
        for i, (feature, stats) in enumerate(
            model_endpoint.status.feature_stats.items()
        ):
            clean_name = self._clean_feature_name(feature)
            clean_feature_stats[clean_name] = stats
            # Exclude the label columns from the feature names
            if (
                model_endpoint.spec.label_names
                and clean_name in model_endpoint.spec.label_names
            ):
                continue
            clean_feature_names.append(clean_name)
        return clean_feature_stats, clean_feature_names

    def patch_model_endpoint(
        self,
        project: str,
        endpoint_id: str,
        attributes: dict,
    ) -> mlrun.common.schemas.ModelEndpoint:
        """
        Update a model endpoint record with a given attributes.

        :param project: The name of the project.
        :param endpoint_id: The unique id of the model endpoint.
        :param attributes: Dictionary of attributes that will be used for update the model endpoint. Note that the keys
                           of the attributes dictionary should exist in the DB table. More details about the model
                           endpoint available attributes can be found under
                           :py:class:`~mlrun.common.schemas.ModelEndpoint`.

        :return: A patched `ModelEndpoint` object.
        """

        # Generate a model endpoint store object and apply the update process
        model_endpoint_store = get_model_endpoint_store(
            project=project,
        )
        model_endpoint_store.update_model_endpoint(
            endpoint_id=endpoint_id, attributes=attributes
        )

        logger.info("Model endpoint table updated", endpoint_id=endpoint_id)

        # Get the patched model endpoint record
        model_endpoint_record = model_endpoint_store.get_model_endpoint(
            endpoint_id=endpoint_id,
        )

        return self._convert_into_model_endpoint_object(endpoint=model_endpoint_record)

    @staticmethod
    def delete_model_endpoint(
        project: str,
        endpoint_id: str,
    ):
        """
        Delete the record of a given model endpoint based on endpoint id.

        :param project:     The name of the project.
        :param endpoint_id: The id of the endpoint.
        """
        model_endpoint_store = get_model_endpoint_store(
            project=project,
        )

        model_endpoint_store.delete_model_endpoint(endpoint_id=endpoint_id)

        logger.info("Model endpoint table cleared", endpoint_id=endpoint_id)

    def get_model_endpoint(
        self,
        auth_info: mlrun.common.schemas.AuthInfo,
        project: str,
        endpoint_id: str,
        metrics: typing.List[str] = None,
        start: str = "now-1h",
        end: str = "now",
        feature_analysis: bool = False,
    ) -> mlrun.common.schemas.ModelEndpoint:
        """Get a single model endpoint object. You can apply different time series metrics that will be added to the
           result.

        :param auth_info:                  The auth info of the request
        :param project:                    The name of the project
        :param endpoint_id:                The unique id of the model endpoint.
        :param metrics:                    A list of metrics to return for the model endpoint. There are pre-defined
                                           metrics for model endpoints such as predictions_per_second and
                                           latency_avg_5m but also custom metrics defined by the user. Please note that
                                           these metrics are stored in the time series DB and the results will be
                                           appeared under `model_endpoint.spec.metrics`.
        :param start:                      The start time of the metrics. Can be represented by a string containing an
                                           RFC 3339 time, a Unix timestamp in milliseconds, a relative time (`'now'` or
                                           `'now-[0-9]+[mhd]'`, where `m` = minutes, `h` = hours, and `'d'` = days), or
                                           0 for the earliest time.
        :param end:                        The end time of the metrics. Can be represented by a string containing an
                                           RFC 3339 time, a Unix timestamp in milliseconds, a relative time (`'now'` or
                                           `'now-[0-9]+[mhd]'`, where `m` = minutes, `h` = hours, and `'d'` = days), or
                                           0 for the earliest time.
        :param feature_analysis:           When True, the base feature statistics and current feature statistics will
                                           be added to the output of the resulting object.

        :return: A `ModelEndpoint` object.
        """

        logger.info(
            "Getting model endpoint record from DB",
            endpoint_id=endpoint_id,
        )

        # Generate a model endpoint store object and get the model endpoint record as a dictionary
        model_endpoint_store = get_model_endpoint_store(
            project=project, access_key=auth_info.data_session
        )

        model_endpoint_record = model_endpoint_store.get_model_endpoint(
            endpoint_id=endpoint_id,
        )

        # Convert to `ModelEndpoint` object
        model_endpoint_object = self._convert_into_model_endpoint_object(
            endpoint=model_endpoint_record, feature_analysis=feature_analysis
        )

        # If time metrics were provided, retrieve the results from the time series DB
        if metrics:
            self._add_real_time_metrics(
                model_endpoint_store=model_endpoint_store,
                model_endpoint_object=model_endpoint_object,
                metrics=metrics,
                start=start,
                end=end,
            )

        return model_endpoint_object

    def list_model_endpoints(
        self,
        auth_info: mlrun.common.schemas.AuthInfo,
        project: str,
        model: str = None,
        function: str = None,
        labels: typing.List[str] = None,
        metrics: typing.List[str] = None,
        start: str = "now-1h",
        end: str = "now",
        top_level: bool = False,
        uids: typing.List[str] = None,
    ) -> mlrun.common.schemas.ModelEndpointList:
        """
        Returns a list of `ModelEndpoint` objects, wrapped in `ModelEndpointList` object. Each `ModelEndpoint`
        object represents the current state of a model endpoint. This functions supports filtering by the following
        parameters:
        1) model
        2) function
        3) labels
        4) top level
        5) uids
        By default, when no filters are applied, all available endpoints for the given project will be listed.

        In addition, this functions provides a facade for listing endpoint related metrics. This facade is time-based
        and depends on the 'start' and 'end' parameters. By default, when the metrics parameter is None, no metrics are
        added to the output of this function.

        :param auth_info: The auth info of the request.
        :param project:   The name of the project.
        :param model:     The name of the model to filter by.
        :param function:  The name of the function to filter by.
        :param labels:    A list of labels to filter by. Label filters work by either filtering a specific value of a
                          label (i.e. list("key=value")) or by looking for the existence of a given key (i.e. "key").
        :param metrics:   A list of metrics to return for each endpoint. There are pre-defined metrics for model
                          endpoints such as `predictions_per_second` and `latency_avg_5m` but also custom metrics
                          defined by the user. Please note that these metrics are stored in the time series DB and the
                          results will be appeared under model_endpoint.spec.metrics of each endpoint.
        :param start:     The start time of the metrics. Can be represented by a string containing an RFC 3339 time,
                          a Unix timestamp in milliseconds, a relative time (`'now'` or `'now-[0-9]+[mhd]'`, where `m`
                          = minutes, `h` = hours, and `'d'` = days), or 0 for the earliest time.
        :param end:       The end time of the metrics. Can be represented by a string containing an RFC 3339 time,
                          a Unix timestamp in milliseconds, a relative time (`'now'` or `'now-[0-9]+[mhd]'`, where `m`
                          = minutes, `h` = hours, and `'d'` = days), or 0 for the earliest time.
        :param top_level: If True, return only routers and endpoints that are NOT children of any router.
        :param uids:      List of model endpoint unique ids to include in the result.

        :return: An object of `ModelEndpointList` which is literally a list of model endpoints along with some metadata.
                 To get a standard list of model endpoints use `ModelEndpointList.endpoints`.
        """

        logger.info(
            "Listing endpoints",
            project=project,
            model=model,
            function=function,
            labels=labels,
            metrics=metrics,
            start=start,
            end=end,
            top_level=top_level,
            uids=uids,
        )

        # Initialize an empty model endpoints list
        endpoint_list = mlrun.common.schemas.model_endpoints.ModelEndpointList(
            endpoints=[]
        )

        # Generate a model endpoint store object and get a list of model endpoint dictionaries
        endpoint_store = get_model_endpoint_store(
            access_key=auth_info.data_session, project=project
        )

        endpoint_dictionary_list = endpoint_store.list_model_endpoints(
            function=function,
            model=model,
            labels=labels,
            top_level=top_level,
            uids=uids,
        )

        for endpoint_dict in endpoint_dictionary_list:

            # Convert to `ModelEndpoint` object
            endpoint_obj = self._convert_into_model_endpoint_object(
                endpoint=endpoint_dict
            )

            # If time metrics were provided, retrieve the results from the time series DB
            if metrics:
                self._add_real_time_metrics(
                    model_endpoint_store=endpoint_store,
                    model_endpoint_object=endpoint_obj,
                    metrics=metrics,
                    start=start,
                    end=end,
                )

            # Add the `ModelEndpoint` object into the model endpoints list
            endpoint_list.endpoints.append(endpoint_obj)

        return endpoint_list

    @staticmethod
    def _add_real_time_metrics(
        model_endpoint_store: mlrun.model_monitoring.stores.ModelEndpointStore,
        model_endpoint_object: mlrun.common.schemas.ModelEndpoint,
        metrics: typing.List[str] = None,
        start: str = "now-1h",
        end: str = "now",
    ) -> mlrun.common.schemas.ModelEndpoint:
        """Add real time metrics from the time series DB to a provided `ModelEndpoint` object. The real time metrics
           will be stored under `ModelEndpoint.status.metrics.real_time`

        :param model_endpoint_store:  `ModelEndpointStore` object that will be used for communicating with the database
                                       and querying the required metrics.
        :param model_endpoint_object: `ModelEndpoint` object that will be filled with the relevant
                                       real time metrics.
        :param metrics:                A list of metrics to return for each endpoint. There are pre-defined metrics for
                                       model endpoints such as `predictions_per_second` and `latency_avg_5m` but also
                                       custom metrics defined by the user. Please note that these metrics are stored in
                                       the time series DB and the results will be appeared under
                                       model_endpoint.spec.metrics of each endpoint.
        :param start:                  The start time of the metrics. Can be represented by a string containing an RFC
                                       3339 time, a Unix timestamp in milliseconds, a relative time (`'now'` or
                                       `'now-[0-9]+[mhd]'`, where `m`= minutes, `h` = hours, and `'d'` = days), or 0
                                       for the earliest time.
        :param end:                    The end time of the metrics. Can be represented by a string containing an RFC
                                       3339 time, a Unix timestamp in milliseconds, a relative time (`'now'` or
                                       `'now-[0-9]+[mhd]'`, where `m`= minutes, `h` = hours, and `'d'` = days), or 0
                                       for the earliest time.

        """
        if model_endpoint_object.status.metrics is None:
            model_endpoint_object.status.metrics = {}

        endpoint_metrics = model_endpoint_store.get_endpoint_real_time_metrics(
            endpoint_id=model_endpoint_object.metadata.uid,
            start=start,
            end=end,
            metrics=metrics,
        )
        if endpoint_metrics:
            model_endpoint_object.status.metrics[
                model_monitoring_constants.EventKeyMetrics.REAL_TIME
            ] = endpoint_metrics
        return model_endpoint_object

    def _convert_into_model_endpoint_object(
        self, endpoint: typing.Dict[str, typing.Any], feature_analysis: bool = False
    ) -> mlrun.common.schemas.ModelEndpoint:
        """
        Create a `ModelEndpoint` object according to a provided model endpoint dictionary.

        :param endpoint:         Dictinoary that represents a DB record of a model endpoint which need to be converted
                                 into a valid `ModelEndpoint` object.
        :param feature_analysis: When True, the base feature statistics and current feature statistics will be added to
                                 the output of the resulting object.

        :return: A `ModelEndpoint` object.
        """

        # Convert into `ModelEndpoint` object
        endpoint_obj = mlrun.common.schemas.ModelEndpoint().from_flat_dict(endpoint)

        # If feature analysis was applied, add feature stats and current stats to the model endpoint result
        if feature_analysis and endpoint_obj.spec.feature_names:

            endpoint_features = self.get_endpoint_features(
                feature_names=endpoint_obj.spec.feature_names,
                feature_stats=endpoint_obj.status.feature_stats,
                current_stats=endpoint_obj.status.current_stats,
            )
            if endpoint_features:
                endpoint_obj.status.features = endpoint_features
                # Add the latest drift measures results (calculated by the model monitoring batch)
                drift_measures = self._json_loads_if_not_none(
                    endpoint.get(
                        model_monitoring_constants.EventFieldType.DRIFT_MEASURES
                    )
                )
                endpoint_obj.status.drift_measures = drift_measures

        return endpoint_obj

    @staticmethod
    def get_endpoint_features(
        feature_names: typing.List[str],
        feature_stats: dict = None,
        current_stats: dict = None,
    ) -> typing.List[mlrun.common.schemas.Features]:
        """
        Getting a new list of features that exist in feature_names along with their expected (feature_stats) and
        actual (current_stats) stats. The expected stats were calculated during the creation of the model endpoint,
        usually based on the data from the Model Artifact. The actual stats are based on the results from the latest
        model monitoring batch job.

        param feature_names: List of feature names.
        param feature_stats: Dictionary of feature stats that were stored during the creation of the model endpoint
                             object.
        param current_stats: Dictionary of the latest stats that were stored during the last run of the model monitoring
                             batch job.

        return: List of feature objects. Each feature has a name, weight, expected values, and actual values. More info
                can be found under `mlrun.common.schemas.Features`.
        """

        # Initialize feature and current stats dictionaries
        safe_feature_stats = feature_stats or {}
        safe_current_stats = current_stats or {}

        # Create feature object and add it to a general features list
        features = []
        for name in feature_names:
            if feature_stats is not None and name not in feature_stats:
                logger.warn("Feature missing from 'feature_stats'", name=name)
            if current_stats is not None and name not in current_stats:
                logger.warn("Feature missing from 'current_stats'", name=name)
            f = mlrun.common.schemas.Features.new(
                name, safe_feature_stats.get(name), safe_current_stats.get(name)
            )
            features.append(f)
        return features

    @staticmethod
    def _json_loads_if_not_none(field: typing.Any) -> typing.Any:
        return (
            json.loads(field)
            if field and field != "null" and field is not None
            else None
        )

    def deploy_monitoring_functions(
        self,
        project: str,
        model_monitoring_access_key: str,
        db_session: sqlalchemy.orm.Session,
        auth_info: mlrun.common.schemas.AuthInfo,
        tracking_policy: mlrun.utils.model_monitoring.TrackingPolicy,
    ):
        """
        Invoking monitoring deploying functions.

        :param project:                     The name of the project.
        :param model_monitoring_access_key: Access key to apply the model monitoring process.
        :param db_session:                  A session that manages the current dialog with the database.
        :param auth_info:                   The auth info of the request.
        :param tracking_policy:             Model monitoring configurations.
        """
        self.deploy_model_monitoring_stream_processing(
            project=project,
            model_monitoring_access_key=model_monitoring_access_key,
            db_session=db_session,
            auth_info=auth_info,
            tracking_policy=tracking_policy,
        )
        self.deploy_model_monitoring_batch_processing(
            project=project,
            model_monitoring_access_key=model_monitoring_access_key,
            db_session=db_session,
            auth_info=auth_info,
            tracking_policy=tracking_policy,
        )

    def verify_project_has_no_model_endpoints(self, project_name: str):
        auth_info = mlrun.common.schemas.AuthInfo(
            data_session=os.getenv("V3IO_ACCESS_KEY")
        )

        if not mlrun.mlconf.igz_version or not mlrun.mlconf.v3io_api:
            return

        endpoints = self.list_model_endpoints(auth_info, project_name)
        if endpoints.endpoints:
            raise mlrun.errors.MLRunPreconditionFailedError(
                f"Project {project_name} can not be deleted since related resources found: model endpoints"
            )

    @staticmethod
    def delete_model_endpoints_resources(project_name: str):
        """
        Delete all model endpoints resources.

        :param project_name: The name of the project.
        """
        auth_info = mlrun.common.schemas.AuthInfo(
            data_session=os.getenv("V3IO_ACCESS_KEY")
        )

        # We would ideally base on config.v3io_api but can't for backwards compatibility reasons,
        # we're using the igz version heuristic
        if not mlrun.mlconf.igz_version or not mlrun.mlconf.v3io_api:
            return

        # Generate a model endpoint store object and get a list of model endpoint dictionaries
        endpoint_store = get_model_endpoint_store(
            access_key=auth_info.data_session, project=project_name
        )
        endpoints = endpoint_store.list_model_endpoints()

        # Delete model endpoints resources from databases using the model endpoint store object
        endpoint_store.delete_model_endpoints_resources(endpoints)

    def deploy_model_monitoring_stream_processing(
        self,
        project: str,
        model_monitoring_access_key: str,
        db_session: sqlalchemy.orm.Session,
        auth_info: mlrun.common.schemas.AuthInfo,
        tracking_policy: mlrun.utils.model_monitoring.TrackingPolicy,
    ):
        """
        Deploying model monitoring stream real time nuclio function. The goal of this real time function is
        to monitor the log of the data stream. It is triggered when a new log entry is detected.
        It processes the new events into statistics that are then written to statistics databases.

        :param project:                     The name of the project.
        :param model_monitoring_access_key: Access key to apply the model monitoring process.
        :param db_session:                  A session that manages the current dialog with the database.
        :param auth_info:                   The auth info of the request.
        :param tracking_policy:             Model monitoring configurations.
        """

        logger.info(
            "Checking if model monitoring stream is already deployed",
            project=project,
        )
        try:
            # validate that the model monitoring stream has not yet been deployed
            mlrun.api.crud.runtimes.nuclio.function.get_nuclio_deploy_status(
                name="model-monitoring-stream",
                project=project,
                tag="",
                auth_info=auth_info,
            )
            logger.info(
                "Detected model monitoring stream processing function already deployed",
                project=project,
            )
            return
        except mlrun.errors.MLRunNotFoundError:
            logger.info(
                "Deploying model monitoring stream processing function", project=project
            )

        # Get parquet target value for model monitoring stream function
        parquet_target = self._get_monitoring_parquet_path(
            db_session=db_session, project=project
        )

        fn = mlrun.model_monitoring.helpers.initial_model_monitoring_stream_processing_function(
            project=project,
            model_monitoring_access_key=model_monitoring_access_key,
            tracking_policy=tracking_policy,
            auth_info=auth_info,
            parquet_target=parquet_target,
        )

        mlrun.api.api.endpoints.functions._build_function(
            db_session=db_session, auth_info=auth_info, function=fn
        )

    def deploy_model_monitoring_batch_processing(
        self,
        project: str,
        model_monitoring_access_key: str,
        db_session: sqlalchemy.orm.Session,
        auth_info: mlrun.common.schemas.AuthInfo,
        tracking_policy: mlrun.utils.model_monitoring.TrackingPolicy,
    ):
        """
        Deploying model monitoring batch job. The goal of this job is to identify drift in the data
        based on the latest batch of events. By default, this job is executed on the hour every hour.
        Note that if the monitoring batch job was already deployed then you will have to delete the
        old monitoring batch job before deploying a new one.

        :param project:                     The name of the project.
        :param model_monitoring_access_key: Access key to apply the model monitoring process.
        :param db_session:                  A session that manages the current dialog with the database.
        :param auth_info:                   The auth info of the request.
        :param tracking_policy:             Model monitoring configurations.
        """

        logger.info(
            "Checking if model monitoring batch processing function is already deployed",
            project=project,
        )

        # Try to list functions that named model monitoring batch
        # to make sure that this job has not yet been deployed
        function_list = mlrun.api.utils.singletons.db.get_db().list_functions(
            session=db_session, name="model-monitoring-batch", project=project
        )

        if function_list:
            logger.info(
                "Detected model monitoring batch processing function already deployed",
                project=project,
            )
            return

        # Create a monitoring batch job function object
        fn = mlrun.model_monitoring.helpers.get_model_monitoring_batch_function(
            project=project,
            model_monitoring_access_key=model_monitoring_access_key,
            db_session=db_session,
            auth_info=auth_info,
            tracking_policy=tracking_policy,
        )

        # Get the function uri
        function_uri = fn.save(versioned=True)
        function_uri = function_uri.replace("db://", "")

        task = mlrun.new_task(name="model-monitoring-batch", project=project)
        task.spec.function = function_uri

        # Apply batching interval params
        interval_list = [
            tracking_policy.default_batch_intervals.minute,
            tracking_policy.default_batch_intervals.hour,
            tracking_policy.default_batch_intervals.day,
        ]
        minutes, hours, days = self._get_batching_interval_param(interval_list)
        batch_dict = {"minutes": minutes, "hours": hours, "days": days}

        task.spec.parameters[
            model_monitoring_constants.EventFieldType.BATCH_INTERVALS_DICT
        ] = batch_dict

        data = {
            "task": task.to_dict(),
            "schedule": self._convert_to_cron_string(
                tracking_policy.default_batch_intervals
            ),
        }

        logger.info(
            "Deploying model monitoring batch processing function", project=project
        )

        # Add job schedule policy (every hour by default)
        mlrun.api.api.utils.submit_run_sync(
            db_session=db_session, auth_info=auth_info, data=data
        )

    @staticmethod
    def _clean_feature_name(feature_name):
        return feature_name.replace(" ", "_").replace("(", "").replace(")", "")

    @staticmethod
    def get_access_key(auth_info: mlrun.common.schemas.AuthInfo):
        """
        Getting access key from the current data session. This method is usually used to verify that the session
        is valid and contains an access key.

        param auth_info: The auth info of the request.

        :return: Access key as a string.
        """
        access_key = auth_info.data_session
        if not access_key:
            raise mlrun.errors.MLRunBadRequestError("Data session is missing")
        return access_key

    @staticmethod
    def _get_batching_interval_param(intervals_list: typing.List):
        """Converting each value in the intervals list into a float number. None
        Values will be converted into 0.0.

        param intervals_list: A list of values based on the ScheduleCronTrigger expression. Note that at the moment
                              it supports minutes, hours, and days. e.g. [0, '*/1', None] represents on the hour
                              every hour.

        :return: A tuple of:
                 [0] = minutes interval as a float
                 [1] = hours interval as a float
                 [2] = days interval as a float
        """
        return tuple(
            [
                0.0
                if isinstance(interval, (float, int)) or interval is None
                else float(f"0{interval.partition('/')[-1]}")
                for interval in intervals_list
            ]
        )

    @staticmethod
    def _convert_to_cron_string(
        cron_trigger: mlrun.common.schemas.schedule.ScheduleCronTrigger,
    ):
        """Converting the batch interval `ScheduleCronTrigger` into a cron trigger expression"""
        return "{} {} {} * *".format(
            cron_trigger.minute, cron_trigger.hour, cron_trigger.day
        ).replace("None", "*")
