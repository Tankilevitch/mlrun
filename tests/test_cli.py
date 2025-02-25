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
import pathlib

import mlrun.projects
from mlrun.__main__ import load_notification


def test_add_notification_to_cli_from_file():
    input_file_path = str(pathlib.Path(__file__).parent / "assets/notification.json")
    notifications = (f"file={input_file_path}",)
    project = mlrun.projects.MlrunProject(name="test")
    load_notification(notifications, project)

    assert (
        project._notifiers._async_notifications["slack"].params.get("webhook")
        == "123456"
    )
    assert (
        project._notifiers._sync_notifications["ipython"].params.get("webhook")
        == "1234"
    )


def test_add_notification_to_cli_from_dict():
    notifications = ('{"slack":{"webhook":"123456"}}', '{"ipython":{"webhook":"1234"}}')
    project = mlrun.projects.MlrunProject(name="test")
    load_notification(notifications, project)

    assert (
        project._notifiers._async_notifications["slack"].params.get("webhook")
        == "123456"
    )
    assert (
        project._notifiers._sync_notifications["ipython"].params.get("webhook")
        == "1234"
    )
