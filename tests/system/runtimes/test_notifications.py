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
import mlrun
import tests.system.base


@tests.system.base.TestMLRunSystem.skip_test_if_env_not_configured
class TestNotifications(tests.system.base.TestMLRunSystem):

    project_name = "notifications-test"

    def test_run_notifications(self):
        error_notification_name = "slack-should-fail"
        success_notification_name = "slack-should-succeed"

        def _assert_notifications():
            runs = self._run_db.list_runs(
                project=self.project_name,
                with_notifications=True,
            )
            assert len(runs) == 1
            assert len(runs[0]["status"]["notifications"]) == 2
            for notification_name, notification in runs[0]["status"][
                "notifications"
            ].items():
                if notification_name == error_notification.name:
                    assert notification["status"] == "error"
                elif notification_name == success_notification.name:
                    assert notification["status"] == "sent"

        error_notification = self._create_notification(
            name=error_notification_name,
            message="should-fail",
            params={
                "webhook": "https://invalid.slack.url.com",
            },
        )
        success_notification = self._create_notification(
            kind="slack",
            name=success_notification_name,
            message="should-succeed",
            params={
                # dummy slack test url should return 200
                "webhook": "https://slack.com/api/api.test",
            },
        )

        function = mlrun.new_function(
            "function-from-module",
            kind="job",
            project=self.project_name,
            image="mlrun/mlrun",
        )
        run = function.run(
            handler="json.dumps",
            params={"obj": {"x": 99}},
            notifications=[error_notification, success_notification],
        )
        assert run.output("return") == '{"x": 99}'

        # the notifications are sent asynchronously, so we need to wait for them
        mlrun.utils.retry_until_successful(
            1,
            40,
            self._logger,
            True,
            _assert_notifications,
        )

    def test_set_run_notifications(self):

        notification_name = "slack-should-succeed"

        def _assert_notification_was_sent():
            runs = self._run_db.list_runs(
                project=self.project_name,
                with_notifications=True,
            )
            assert len(runs) == 1
            assert len(runs[0]["status"]["notifications"]) == 1
            assert (
                runs[0]["status"]["notifications"][notification_name]["status"]
                == "sent"
            )

        self._create_sleep_func_in_project()

        notification = self._create_notification(
            name=notification_name,
            message="should-succeed",
            params={
                # dummy slack test url should return 200
                "webhook": "https://slack.com/api/api.test",
            },
        )

        run = self.project.run_function(
            "test-sleep", local=False, params={"time_to_sleep": 10}
        )
        self._run_db.set_run_notifications(
            self.project_name, run.metadata.uid, [notification]
        )

        run.wait_for_completion()

        # the notifications are sent asynchronously, so we need to wait for them
        mlrun.utils.retry_until_successful(
            1,
            40,
            self._logger,
            True,
            _assert_notification_was_sent,
        )

    def test_set_schedule_notifications(self):

        notification_name = "slack-notification"
        schedule_name = "test-sleep"

        def _assert_notification_in_schedule():
            schedule = self._run_db.get_schedule(
                self.project_name, schedule_name, include_last_run=True
            )
            schedule_spec = schedule.scheduled_object["task"]["spec"]
            last_run = schedule.last_run
            assert "notifications" in schedule_spec
            assert len(schedule_spec["notifications"]) == 1
            assert schedule_spec["notifications"][0]["name"] == notification_name

            runs = self._run_db.list_runs(
                uid=last_run["metadata"]["uid"],
                project=self.project_name,
                with_notifications=True,
            )
            assert len(runs) == 1
            assert len(runs[0]["status"]["notifications"]) == 1
            assert (
                runs[0]["status"]["notifications"][notification_name]["status"]
                == "sent"
            )

        self._create_sleep_func_in_project()

        notification = self._create_notification(
            name=notification_name,
            message="should-succeed",
            params={
                # dummy slack test url should return 200
                "webhook": "https://slack.com/api/api.test",
            },
        )

        self.project.run_function(
            "test-sleep",
            local=False,
            params={"time_to_sleep": 1},
            schedule="* * * * *",
        )
        self._run_db.set_schedule_notifications(
            self.project_name, schedule_name, [notification]
        )

        mlrun.utils.retry_until_successful(
            1,
            2 * 60,  # 2 schedule cycles, so at least one should run
            self._logger,
            True,
            _assert_notification_in_schedule,
        )

    @staticmethod
    def _create_notification(
        kind=None,
        name=None,
        message=None,
        severity=None,
        when=None,
        condition=None,
        params=None,
    ):
        return mlrun.model.Notification(
            kind=kind or "slack",
            when=when or ["completed"],
            name=name or "test-notification",
            message=message or "test-notification-message",
            condition=condition,
            severity=severity or "info",
            params=params or {},
        )

    def _create_sleep_func_in_project(self):

        code_path = str(self.assets_path / "sleep.py")

        sleep_func = mlrun.code_to_function(
            name="test-sleep",
            kind="job",
            project=self.project_name,
            filename=code_path,
            image="mlrun/mlrun",
        )
        self.project.set_function(sleep_func)
        self.project.sync_functions(save=True)

        return sleep_func
