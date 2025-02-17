"""DatabricksWorkflowTaskGroup for submitting jobs to Databricks."""
from __future__ import annotations

from logging import Logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass
import json
import time
from typing import Any

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.providers.databricks.hooks.databricks import DatabricksHook
from airflow.utils.context import Context
from airflow.utils.task_group import TaskGroup

try:
    from attrs import define
except ModuleNotFoundError:
    from attr import define

from databricks_cli.jobs.api import JobsApi
from databricks_cli.runs.api import RunsApi
from databricks_cli.sdk.api_client import ApiClient
from mergedeep import merge

from astro_databricks.plugins.plugin import (
    DatabricksJobRepairAllFailedLink,
    DatabricksJobRunLink,
)


@define
class DatabricksMetaData:
    databricks_conn_id: str
    databricks_run_id: str
    databricks_job_id: str


def _get_job_by_name(job_name: str, jobs_api: JobsApi) -> dict | None:
    jobs = jobs_api.list_jobs().get("jobs", [])
    for job in jobs:
        if job.get("settings", {}).get("name") == job_name:
            return job
    return None


def flatten_node(
    node: TaskGroup | BaseOperator, tasks: list[BaseOperator] = []
) -> list[BaseOperator]:
    """
    Flattens a node (either a TaskGroup or Operator) to a list of nodes
    """
    if isinstance(node, BaseOperator):
        return [node]

    if isinstance(node, TaskGroup):
        new_tasks = []
        for id_, child in node.children.items():
            new_tasks += flatten_node(child, tasks)

        return tasks + new_tasks

    return tasks


class _CreateDatabricksWorkflowOperator(BaseOperator):
    """Creates a databricks workflow from a DatabricksWorkflowTaskGroup.

    :param task_id: The task id of the operator
    :param databricks_conn_id: The databricks connection id
    :param job_clusters: A list of job clusters to use in the workflow
    :param existing_clusters: A list of existing clusters to use in the workflow
    :param max_concurrent_runs: The maximum number of concurrent runs
    :param tasks_to_convert: A list of tasks to convert to a workflow. This list can also
    be populated after initialization by calling add_task.
    :param extra_job_params: A dictionary containing properties which will override the
    default Databricks Workflow Job definitions.
    :param notebook_params:  A dictionary of notebook parameters to pass to the workflow.These parameters will be passed to
    all notebook tasks in the workflow.
    """

    template_fields = ("notebook_params",)

    operator_extra_links = (DatabricksJobRunLink(), DatabricksJobRepairAllFailedLink())
    databricks_conn_id: str
    databricks_run_id: str
    databricks_job_id: str

    def __init__(
        self,
        task_id,
        databricks_conn_id,
        job_clusters: list[dict[str, object]] = None,
        existing_clusters: list[str] = None,
        max_concurrent_runs: int = 1,
        tasks_to_convert: list[BaseOperator] = None,
        extra_job_params: dict[str, Any] = None,
        notebook_params: dict | None = None,
        **kwargs,
    ):
        self.existing_clusters = existing_clusters or []
        self.job_clusters = job_clusters or []
        self.job_cluster_dict = {j["job_cluster_key"]: j for j in self.job_clusters}
        self.tasks_to_convert = tasks_to_convert or []
        self.relevant_upstreams = [task_id]
        self.databricks_conn_id = databricks_conn_id
        self.databricks_run_id = None
        self.max_concurrent_runs = max_concurrent_runs
        self.extra_job_params = extra_job_params or {}
        self.notebook_params = notebook_params or {}
        super().__init__(task_id=task_id, **kwargs)

        # For Airflow versions <2.3, the `task_group` attribute is unassociated, and hence we need to add that.
        if not hasattr(self, "task_group"):
            from airflow.utils.task_group import TaskGroupContext

            self.task_group = TaskGroupContext.get_current_task_group(self.dag)

    def add_task(self, task: BaseOperator):
        """
        Add a task to the list of tasks to convert to a workflow.

        :param task:
        :return:
        """
        self.tasks_to_convert.append(task)

    def create_workflow_json(self, context: Context | None = None) -> dict[str, object]:
        """Create a workflow json that can be submitted to databricks.

        :return: A workflow json
        """
        task_json = [
            task.convert_to_databricks_workflow_task(
                relevant_upstreams=self.relevant_upstreams, context=context
            )
            for task in self.tasks_to_convert
        ]
        default_json = {
            "name": self.databricks_job_name,
            "email_notifications": {"no_alert_for_skipped_runs": False},
            "timeout_seconds": 0,
            "tasks": task_json,
            "format": "MULTI_TASK",
            "job_clusters": self.job_clusters,
            "max_concurrent_runs": self.max_concurrent_runs,
        }
        merged_json = merge(default_json, self.extra_job_params)
        return merged_json

    @property
    def databricks_job_name(self):
        return self.dag_id + "." + self.task_group.group_id

    def execute(self, context: Context) -> Any:
        hook = DatabricksHook(self.databricks_conn_id)
        databricks_conn = hook.get_conn()
        api_client = ApiClient(
            token=databricks_conn.password, host=databricks_conn.host
        )
        jobs_api = JobsApi(api_client)
        job = _get_job_by_name(self.databricks_job_name, jobs_api)

        job_id = job["job_id"] if job else None
        current_job_spec = self.create_workflow_json(context)
        if not isinstance(self.task_group, DatabricksWorkflowTaskGroup):
            raise AirflowException("Task group must be a DatabricksWorkflowTaskGroup")
        if job_id:
            self.log.info(
                "Updating existing job with spec %s",
                json.dumps(current_job_spec, indent=4),
            )

            jobs_api.reset_job(
                json={"job_id": job_id, "new_settings": current_job_spec}
            )
        else:
            self.log.info(
                "Creating new job with spec %s", json.dumps(current_job_spec, indent=4)
            )
            job_id = jobs_api.create_job(json=current_job_spec)["job_id"]

        run_id = jobs_api.run_now(
            job_id=job_id,
            jar_params=self.task_group.jar_params,
            notebook_params=self.notebook_params,
            python_params=self.task_group.python_params,
            spark_submit_params=self.task_group.spark_submit_params,
        )["run_id"]
        self.databricks_run_id = run_id

        runs_api = RunsApi(api_client)
        url = runs_api.get_run(run_id).get("run_page_url")
        self.log.info(f"Check the job run in Databricks: {url}")
        state = runs_api.get_run(run_id)["state"]["life_cycle_state"]
        self.log.info(f"Job state: {state}")

        if state not in ("PENDING", "BLOCKED", "RUNNING"):
            raise AirflowException(
                f"Could not start the workflow job, it had state {state}"
            )

        while state in ("PENDING", "BLOCKED"):
            self.log.info(f"Job {state}")
            time.sleep(5)
            state = runs_api.get_run(run_id)["state"]["life_cycle_state"]

        return {
            "databricks_conn_id": self.databricks_conn_id,
            "databricks_job_id": job_id,
            "databricks_run_id": run_id,
        }


class DatabricksWorkflowTaskGroup(TaskGroup):
    """
    A task group that takes a list of tasks and creates a databricks workflow.

    The DatabricksWorkflowTaskGroup takes a list of tasks and creates a databricks workflow
    based on the metadata produced by those tasks. For a task to be eligible for this
    TaskGroup, it must contain the ``convert_to_databricks_workflow_task`` method. If any tasks
    do not contain this method then the Taskgroup will raise an error at parse time.

    Here is an example of what a DAG looks like with a DatabricksWorkflowTaskGroup:

    .. code-block:: python

        job_clusters = [
            {
                "job_cluster_key": "Shared_job_cluster",
                "new_cluster": {
                    "cluster_name": "",
                    "spark_version": "11.3.x-scala2.12",
                    "aws_attributes": {
                        "first_on_demand": 1,
                        "availability": "SPOT_WITH_FALLBACK",
                        "zone_id": "us-east-2b",
                        "spot_bid_price_percent": 100,
                        "ebs_volume_count": 0,
                    },
                    "node_type_id": "i3.xlarge",
                    "spark_env_vars": {"PYSPARK_PYTHON": "/databricks/python3/bin/python3"},
                    "enable_elastic_disk": False,
                    "data_security_mode": "LEGACY_SINGLE_USER_STANDARD",
                    "runtime_engine": "STANDARD",
                    "num_workers": 8,
                },
            }
        ]

    with dag:
        task_group = DatabricksWorkflowTaskGroup(
            group_id="test_workflow",
            databricks_conn_id="databricks_conn",
            job_clusters=job_cluster_spec,
            notebook_params={},
            notebook_packages=[
                {
                    "pypi": {
                        "package": "simplejson"
                    }
                },
            ]
        )
        with task_group:
            notebook_1 = DatabricksNotebookOperator(
                task_id="notebook_1",
                databricks_conn_id="databricks_conn",
                notebook_path="/Users/<user>/Test workflow",
                source="WORKSPACE",
                job_cluster_key="Shared_job_cluster",
                notebook_packages=[
                    {
                        "pypi": {
                            "package": "Faker"
                        }
                    }
                ]
            )
            notebook_2 = DatabricksNotebookOperator(
                task_id="notebook_2",
                databricks_conn_id="databricks_conn",
                notebook_path="/Users/<user>/Test workflow",
                source="WORKSPACE",
                job_cluster_key="Shared_job_cluster",
                notebook_params={
                    "foo": "bar",
                },
            )
            notebook_1 >> notebook_2

    With this example, Airflow will produce a job named <dag_name>.test_workflow that will
    run notebook_1 and then notebook_2. The job will be created in the databricks workspace
    if it does not already exist. If the job already exists, it will be updated to match
    the workflow defined in the DAG.

    To minimize update conflicts, we recommend that you keep parameters in the ``notebook_params`` of the
    ``DatabricksWorkflowTaskGroup`` and not in the ``DatabricksNotebookOperator`` whenever possible.
        This is because tasks in the
    ``DatabricksWorkflowTaskGroup`` are passed in at the job trigger time and do not modify the job definition

    :param group_id: The name of the task group
    :param databricks_conn_id: The name of the databricks connection to use
    :param job_clusters: A list of job clusters to use for this workflow.
    :param notebook_params: A dictionary of notebook parameters to pass to the workflow. These parameters will be passed to
    all notebook tasks in the workflow.
    :param notebook_packages: A list of dictionary of Python packages to be installed. Packages defined at the
    workflow task group level are installed for each of the notebook tasks under it. And packages defined at the
    notebook task level are installed specific for the notebook task.
    :param jar_params: A list of jar parameters to pass to the workflow. These parameters will be passed to all jar
        tasks
    in the workflow.
    :param python_params: A list of python parameters to pass to the workflow. These parameters will be passed to
        all python tasks
    in the workflow.
    :param spark_submit_params: A list of spark submit parameters to pass to the workflow. These parameters
        will be passed to all spark submit tasks
    :param extra_job_params: A dictionary containing properties which will override the default Databricks Workflow
    Job definitions.
    :param max_concurrent_runs: The maximum number of concurrent runs for this workflow.

    """

    @property
    def log(self) -> Logger:
        """Returns logger."""
        pass

    is_databricks = True

    def __init__(
        self,
        databricks_conn_id,
        existing_clusters=None,
        job_clusters=None,
        jar_params: dict = None,
        notebook_params: dict | None = None,
        notebook_packages: list[dict[str, Any]] = None,
        python_params: list = None,
        spark_submit_params: list = None,
        max_concurrent_runs: int = 1,
        extra_job_params: dict[str, Any] = None,
        **kwargs,
    ):
        """
        Create a new DatabricksWorkflowTaskGroup.

        :param group_id: The name of the task group
        :param databricks_conn_id: The name of the databricks connection to use
        :param job_clusters: A list of job clusters to use for this workflow.
        :param notebook_params: A dictionary of notebook parameters to pass to the workflow.These parameters will be passed to
        all notebook tasks in the workflow.
        :param notebook_packages: A list of dictionary of Python packages to be installed. These packages will be passed
         to all notebook tasks in the workflow.
        :param jar_params: A list of jar parameters to pass to the workflow.
         These parameters will be passed to all jar tasks
        in the workflow.
        :param python_params: A dictionary of python parameters to pass to the workflow.
         These parameters will be passed to all python tasks
        in the workflow.
        :param spark_submit_params: A list of spark submit parameters to pass to the workflow.
         These parameters will be passed to all spark submit tasks
        :param max_concurrent_runs: The maximum number of concurrent runs for this workflow.
        :param extra_job_params: A dictionary containing properties which will override the default Databricks
        Workflow Job definitions.
        """
        self.databricks_conn_id = databricks_conn_id
        self.existing_clusters = existing_clusters or []
        self.job_clusters = job_clusters or []
        self.notebook_params = notebook_params or {}
        self.notebook_packages = notebook_packages or []
        self.python_params = python_params or []
        self.spark_submit_params = spark_submit_params or []
        self.jar_params = jar_params or []
        self.max_concurrent_runs = max_concurrent_runs
        self.extra_job_params = extra_job_params or {}
        super().__init__(**kwargs)

    def __exit__(self, _type, _value, _tb):
        """Exit the context manager and add tasks to a single _CreateDatabricksWorkflowOperator."""
        roots = list(self.get_roots())
        tasks = flatten_node(self)

        # For Airflow versions <2.3, the `dag` attribute is unassociated, and hence we need to add that.
        if not hasattr(self, "dag"):
            from airflow.models.dag import DagContext

            self.dag = DagContext.get_current_dag()

        create_databricks_workflow_task = _CreateDatabricksWorkflowOperator(
            dag=self.dag,
            task_group=self,
            task_id="launch",
            databricks_conn_id=self.databricks_conn_id,
            job_clusters=self.job_clusters,
            existing_clusters=self.existing_clusters,
            extra_job_params=self.extra_job_params,
            notebook_params=self.notebook_params,
        )

        for task in tasks:
            if not (
                hasattr(task, "convert_to_databricks_workflow_task")
                and callable(task.convert_to_databricks_workflow_task)
            ):
                raise AirflowException(
                    f"Task {task.task_id} does not support conversion to databricks workflow task."
                )

            task.databricks_metadata = create_databricks_workflow_task.output
            create_databricks_workflow_task.relevant_upstreams.append(task.task_id)
            create_databricks_workflow_task.add_task(task)

        for root_task in roots:
            root_task.set_upstream(create_databricks_workflow_task)

        super().__exit__(_type, _value, _tb)
