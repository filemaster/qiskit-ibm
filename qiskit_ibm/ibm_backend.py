# This code is part of Qiskit.
#
# (C) Copyright IBM 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Module for interfacing with an IBM Quantum Backend."""

import logging
import warnings
import copy

from typing import Dict, List, Union, Optional, Any
from datetime import datetime as python_datetime

from qiskit.compiler import assemble
from qiskit.circuit import QuantumCircuit, Parameter, Delay
from qiskit.circuit.duration import duration_in_dt
from qiskit.pulse import Schedule, LoConfig
from qiskit.pulse.channels import PulseChannel
from qiskit.qobj import QasmQobj, PulseQobj
from qiskit.qobj.utils import MeasLevel, MeasReturnType
from qiskit.providers.backend import BackendV1 as Backend
from qiskit.providers.options import Options
from qiskit.providers.models import (BackendStatus, BackendProperties,
                                     PulseDefaults, GateConfig)
from qiskit.tools.events.pubsub import Publisher
from qiskit.providers.models import (QasmBackendConfiguration,
                                     PulseBackendConfiguration)

from qiskit_ibm import ibm_provider  # pylint: disable=unused-import

from .apiconstants import ApiJobStatus, API_JOB_FINAL_STATES
from .api.clients import AccountClient
from .api.exceptions import ApiError
from .backendjoblimit import BackendJobLimit
from .backendreservation import BackendReservation
from .credentials import Credentials
from .exceptions import (IBMBackendError, IBMBackendValueError, IBMBackendJobLimitError,
                         IBMBackendApiError, IBMBackendApiProtocolError)
from .job import IBMJob, IBMCircuitJob, IBMCompositeJob
from .utils import validate_job_tags
from .utils.converters import utc_to_local_all, local_to_utc
from .utils.json_decoder import decode_pulse_defaults, decode_backend_properties
from .utils.backend import convert_reservation_data
from .utils.utils import api_status_to_job_status

logger = logging.getLogger(__name__)


class IBMBackend(Backend):
    """Backend class interfacing with an IBM Quantum device.

    You can run experiments on a backend using the :meth:`run()` method. The
    :meth:`run()` method takes one or more :class:`~qiskit.circuit.QuantumCircuit`
    or :class:`~qiskit.pulse.Schedule` and returns
    an :class:`~qiskit_ibm.job.IBMJob`
    instance that represents the submitted job. Each job has a unique job ID, which
    can later be used to retrieve the job. An example of this flow::

        from qiskit import transpile
        from qiskit_ibm import IBMProvider
        from qiskit.circuit.random import random_circuit

        provider = IBMProvider()
        backend = provider.backend.ibmq_vigo
        qx = random_circuit(n_qubits=5, depth=4)
        transpiled = transpile(qx, backend=backend)
        job = backend.run(transpiled)
        retrieved_job = provider.backend.job(job.job_id())

    Note:

        * Unlike :meth:`qiskit.execute`, the :meth:`run` method does not transpile
          the circuits/schedules for you, so be sure to do so before submitting them.

        * You should not instantiate the ``IBMBackend`` class directly. Instead, use
          the methods provided by an :class:`IBMProvider` instance to retrieve and handle
          backends.

    Other methods return information about the backend. For example, the :meth:`status()` method
    returns a :class:`BackendStatus<qiskit.providers.models.BackendStatus>` instance.
    The instance contains the ``operational`` and ``pending_jobs`` attributes, which state whether
    the backend is operational and also the number of jobs in the server queue for the backend,
    respectively::

        status = backend.status()
        is_operational = status.operational
        jobs_in_queue = status.pending_jobs

    It is also possible to see the number of remaining jobs you are able to submit to the
    backend with the :meth:`job_limit()` method, which returns a
    :class:`BackendJobLimit<qiskit_ibm.BackendJobLimit>` instance::

        job_limit = backend.job_limit()
    """

    id_warning_issued = False

    def __init__(
            self,
            configuration: Union[QasmBackendConfiguration, PulseBackendConfiguration],
            provider: 'ibm_provider.IBMProvider',
            credentials: Credentials,
            api_client: AccountClient
    ) -> None:
        """IBMBackend constructor.

        Args:
            configuration: Backend configuration.
            provider: IBM Quantum account provider
            credentials: IBM Quantum credentials.
            api_client: IBM Quantum client used to communicate with the server.
        """
        super().__init__(provider=provider, configuration=configuration)

        self._api_client = api_client
        self._credentials = credentials
        self.hub = credentials.hub
        self.group = credentials.group
        self.project = credentials.project

        # Attributes used by caching functions.
        self._properties = None
        self._defaults = None

    @classmethod
    def _default_options(cls) -> Options:
        """Default runtime options."""
        return Options(shots=4000, memory=False,
                       qubit_lo_freq=None, meas_lo_freq=None,
                       schedule_los=None,
                       meas_level=MeasLevel.CLASSIFIED,
                       meas_return=MeasReturnType.AVERAGE,
                       memory_slots=None, memory_slot_size=100,
                       rep_time=None, rep_delay=None,
                       init_qubits=True, use_measure_esp=None,
                       live_data_enabled=None)

    def run(
            self,
            circuits: Union[QuantumCircuit, Schedule,
                            List[Union[QuantumCircuit, Schedule]]],
            job_name: Optional[str] = None,
            job_tags: Optional[List[str]] = None,
            max_circuits_per_job: Optional[int] = None,
            header: Optional[Dict] = None,
            shots: Optional[int] = None,
            memory: Optional[bool] = None,
            qubit_lo_freq: Optional[List[int]] = None,
            meas_lo_freq: Optional[List[int]] = None,
            schedule_los: Optional[Union[List[Union[Dict[PulseChannel, float], LoConfig]],
                                         Union[Dict[PulseChannel, float], LoConfig]]] = None,
            meas_level: Optional[Union[int, MeasLevel]] = None,
            meas_return: Optional[Union[str, MeasReturnType]] = None,
            memory_slots: Optional[int] = None,
            memory_slot_size: Optional[int] = None,
            rep_time: Optional[int] = None,
            rep_delay: Optional[float] = None,
            init_qubits: Optional[bool] = None,
            parameter_binds: Optional[List[Dict[Parameter, float]]] = None,
            use_measure_esp: Optional[bool] = None,
            live_data_enabled: Optional[bool] = None,
            **run_config: Dict
    ) -> IBMJob:
        """Run on the backend.

        If a keyword specified here is also present in the ``options`` attribute/object,
        the value specified here will be used for this run.

        If the length of the input circuits exceeds the maximum allowed by
        the backend, or if `max_circuits_per_job` is not ``None``, then the
        input circuits will be divided into multiple jobs, and an
        :class:`~qiskit_ibm.job.IBMCompositeJob` instance is
        returned.

        Args:
            circuits: An individual or a
                list of :class:`~qiskit.circuits.QuantumCircuit` or
                :class:`~qiskit.pulse.Schedule` objects to run on the backend.
            job_name: Custom name to be assigned to the job. This job
                name can subsequently be used as a filter in the
                :meth:`jobs()` method. Job names do not need to be unique.
            job_tags: Tags to be assigned to the job. The tags can subsequently be used
                as a filter in the :meth:`jobs()` function call.
            max_circuits_per_job: Maximum number of circuits to have in a single job.

            header: User input that will be attached to the job and will be
                copied to the corresponding result header. Headers do not affect the run.
                This replaces the old ``Qobj`` header.
            shots: Number of repetitions of each circuit, for sampling. Default: 4000
                or ``max_shots`` from the backend configuration, whichever is smaller.
            memory: If ``True``, per-shot measurement bitstrings are returned as well
                (provided the backend supports it). For OpenPulse jobs, only
                measurement level 2 supports this option.
            qubit_lo_freq: List of default qubit LO frequencies in Hz. Will be overridden by
                ``schedule_los`` if set.
            meas_lo_freq: List of default measurement LO frequencies in Hz. Will be overridden
                by ``schedule_los`` if set.
            schedule_los: Experiment LO configurations, frequencies are given in Hz.
            meas_level: Set the appropriate level of the measurement output for pulse experiments.
            meas_return: Level of measurement data for the backend to return.

                For ``meas_level`` 0 and 1:
                    * ``single`` returns information from every shot.
                    * ``avg`` returns average measurement output (averaged over number of shots).
            memory_slots: Number of classical memory slots to use.
            memory_slot_size: Size of each memory slot if the output is Level 0.
            rep_time: Time per program execution in seconds. Must be from the list provided
                by the backend (``backend.configuration().rep_times``).
                Defaults to the first entry.
            rep_delay: Delay between programs in seconds. Only supported on certain
                backends (if ``backend.configuration().dynamic_reprate_enabled=True``).
                If supported, ``rep_delay`` will be used instead of ``rep_time`` and must be
                from the range supplied
                by the backend (``backend.configuration().rep_delay_range``). Default is given by
                ``backend.configuration().default_rep_delay``.
            init_qubits: Whether to reset the qubits to the ground state for each shot.
                Default: ``True``.
            parameter_binds: List of Parameter bindings over which the set of experiments will be
                executed. Each list element (bind) should be of the form
                {Parameter1: value1, Parameter2: value2, ...}. All binds will be
                executed across all experiments; e.g., if parameter_binds is a
                length-n list, and there are m experiments, a total of m x n
                experiments will be run (one for each experiment/bind pair).
            use_measure_esp: Whether to use excited state promoted (ESP) readout for measurements
                which are the terminal instruction to a qubit. ESP readout can offer higher fidelity
                than standard measurement sequences. See
                `here <https://arxiv.org/pdf/2008.08571.pdf>`_.
                Default: ``True`` if backend supports ESP readout, else ``False``. Backend support
                for ESP readout is determined by the flag ``measure_esp_enabled`` in
                ``backend.configuration()``.
            live_data_enabled (bool): Activate the live data in the backend, to receive data
                from the instruments.
            **run_config: Extra arguments used to configure the run.

        Returns:
            The job to be executed.

        Raises:
            IBMBackendApiError: If an unexpected error occurred while submitting
                the job.
            IBMBackendApiProtocolError: If an unexpected value received from
                 the server.
            IBMBackendValueError:
                - If an input parameter value is not valid.
                - If ESP readout is used and the backend does not support this.
        """
        # pylint: disable=arguments-differ
        validate_job_tags(job_tags, IBMBackendValueError)

        sim_method = None
        if self.configuration().simulator:
            sim_method = getattr(self.configuration(), 'simulation_method', None)

        measure_esp_enabled = getattr(self.configuration(), "measure_esp_enabled", False)
        # set ``use_measure_esp`` to backend value if not set by user
        if use_measure_esp is None:
            use_measure_esp = measure_esp_enabled
        if use_measure_esp and not measure_esp_enabled:
            raise IBMBackendValueError(
                "ESP readout not supported on this device. Please make sure the flag "
                "'use_measure_esp' is unset or set to 'False'."
            )

        if not self.configuration().simulator:
            self._deprecate_id_instruction(circuits)

        run_config_dict = self._get_run_config(
            qobj_header=header,
            shots=shots,
            memory=memory,
            qubit_lo_freq=qubit_lo_freq,
            meas_lo_freq=meas_lo_freq,
            schedule_los=schedule_los,
            meas_level=meas_level,
            meas_return=meas_return,
            memory_slots=memory_slots,
            memory_slot_size=memory_slot_size,
            rep_time=rep_time,
            rep_delay=rep_delay,
            init_qubits=init_qubits,
            use_measure_esp=use_measure_esp,
            **run_config)
        if parameter_binds:
            run_config_dict['parameter_binds'] = parameter_binds
        if sim_method and 'method' not in run_config_dict:
            run_config_dict['method'] = sim_method

        if isinstance(circuits, list):
            chunk_size = None
            if hasattr(self.configuration(), 'max_experiments'):
                backend_max = self.configuration().max_experiments
                chunk_size = backend_max if max_circuits_per_job is None \
                    else min(backend_max, max_circuits_per_job)
            elif max_circuits_per_job:
                chunk_size = max_circuits_per_job

            if chunk_size and len(circuits) > chunk_size:
                circuits_list = [circuits[x:x + chunk_size]
                                 for x in range(0, len(circuits), chunk_size)]
                return IBMCompositeJob(backend=self, api_client=self._api_client,
                                       circuits_list=circuits_list,
                                       run_config=run_config_dict,
                                       name=job_name,
                                       tags=job_tags)

        qobj = assemble(circuits, self, **run_config_dict)

        return self._submit_job(qobj=qobj,
                                job_name=job_name,
                                job_tags=job_tags,
                                live_data_enabled=live_data_enabled)

    def _get_run_config(self, **kwargs: Any) -> Dict:
        """Return the consolidated runtime configuration."""
        run_config_dict = copy.copy(self.options.__dict__)
        for key, val in kwargs.items():
            if val is not None:
                run_config_dict[key] = val
                if key not in self.options.__dict__ and not isinstance(self, IBMSimulator):
                    warnings.warn(f"{key} is not a recognized runtime"  # type: ignore[unreachable]
                                  f" option and may be ignored by the backend.", stacklevel=4)
        return run_config_dict

    def _submit_job(
            self,
            qobj: Union[QasmQobj, PulseQobj],
            job_name: Optional[str] = None,
            job_tags: Optional[List[str]] = None,
            composite_job_id: Optional[str] = None,
            live_data_enabled: Optional[bool] = None
    ) -> IBMJob:
        """Submit the Qobj to the backend.

        Args:
            qobj: The Qobj to be executed.
            job_name: Custom name to be assigned to the job. This job
                name can subsequently be used as a filter in the
                ``jobs()``method.
                Job names do not need to be unique.
            job_tags: Tags to be assigned to the job.
            composite_job_id: Composite job ID, if this Qobj belongs to a composite job.
            live_data_enabled: Used to activate/deactivate live data on the backend.

        Returns:
            The job to be executed.

        Events:
            ibm.job.start: The job has started.

        Raises:
            IBMBackendApiError: If an unexpected error occurred while submitting
                the job.
            IBMBackendError: If an unexpected error occurred after submitting
                the job.
            IBMBackendApiProtocolError: If an unexpected value is received from
                 the server.
            IBMBackendJobLimitError: If the job could not be submitted because
                the job limit has been reached.
        """
        try:
            qobj_dict = qobj.to_dict()
            submit_info = self._api_client.job_submit(
                backend_name=self.name(),
                qobj_dict=qobj_dict,
                job_name=job_name,
                job_tags=job_tags,
                experiment_id=composite_job_id,
                live_data_enabled=live_data_enabled)
        except ApiError as ex:
            if 'Error code: 3458' in str(ex):
                raise IBMBackendJobLimitError('Error submitting job: {}'.format(str(ex))) from ex
            raise IBMBackendApiError('Error submitting job: {}'.format(str(ex))) from ex

        # Error in the job after submission:
        # Transition to the `ERROR` final state.
        if 'error' in submit_info:
            raise IBMBackendError(
                'Error submitting job: {}'.format(str(submit_info['error'])))

        # Submission success.
        try:
            job = IBMCircuitJob(backend=self, api_client=self._api_client,
                                qobj=qobj, **submit_info)
            logger.debug('Job %s was successfully submitted.', job.job_id())
        except TypeError as err:
            logger.debug("Invalid job data received: %s", submit_info)
            raise IBMBackendApiProtocolError('Unexpected return value received from the server '
                                             'when submitting job: {}'.format(str(err))) from err
        Publisher().publish("ibm.job.start", job)
        return job

    def properties(
            self,
            refresh: bool = False,
            datetime: Optional[python_datetime] = None
    ) -> Optional[BackendProperties]:
        """Return the backend properties, subject to optional filtering.

        This data describes qubits properties (such as T1 and T2),
        gates properties (such as gate length and error), and other general
        properties of the backend.

        The schema for backend properties can be found in
        `Qiskit/ibm-quantum-schemas
        <https://github.com/Qiskit/ibm-quantum-schemas/blob/main/schemas/backend_properties_schema.json>`_.

        Args:
            refresh: If ``True``, re-query the server for the backend properties.
                Otherwise, return a cached version.
            datetime: By specifying `datetime`, this function returns an instance
                of the :class:`BackendProperties<qiskit.providers.models.BackendProperties>`
                whose timestamp is closest to, but older than, the specified `datetime`.

        Returns:
            The backend properties or ``None`` if the backend properties are not
            currently available.

        Raises:
            TypeError: If an input argument is not of the correct type.
        """
        # pylint: disable=arguments-differ
        if not isinstance(refresh, bool):
            raise TypeError("The 'refresh' argument needs to be a boolean. "
                            "{} is of type {}".format(refresh, type(refresh)))
        if datetime and not isinstance(datetime, python_datetime):
            raise TypeError("'{}' is not of type 'datetime'.")

        if datetime:
            datetime = local_to_utc(datetime)

        if datetime or refresh or self._properties is None:
            api_properties = self._api_client.backend_properties(self.name(), datetime=datetime)
            if not api_properties:
                return None
            decode_backend_properties(api_properties)
            api_properties = utc_to_local_all(api_properties)
            backend_properties = BackendProperties.from_dict(api_properties)
            if datetime:    # Don't cache result.
                return backend_properties
            self._properties = backend_properties
        return self._properties

    def status(self) -> BackendStatus:
        """Return the backend status.

        Note:
            If the returned :class:`~qiskit.providers.models.BackendStatus`
            instance has ``operational=True`` but ``status_msg="internal"``,
            then the backend is accepting jobs but not processing them.

        Returns:
            The status of the backend.

        Raises:
            IBMBackendApiProtocolError: If the status for the backend cannot be formatted properly.
        """
        api_status = self._api_client.backend_status(self.name())

        try:
            return BackendStatus.from_dict(api_status)
        except TypeError as ex:
            raise IBMBackendApiProtocolError(
                'Unexpected return value received from the server when '
                'getting backend status: {}'.format(str(ex))) from ex

    def defaults(self, refresh: bool = False) -> Optional[PulseDefaults]:
        """Return the pulse defaults for the backend.

        The schema for default pulse configuration can be found in
        `Qiskit/ibm-quantum-schemas
        <https://github.com/Qiskit/ibm-quantum-schemas/blob/main/schemas/default_pulse_configuration_schema.json>`_.

        Args:
            refresh: If ``True``, re-query the server for the backend pulse defaults.
                Otherwise, return a cached version.

        Returns:
            The backend pulse defaults or ``None`` if the backend does not support pulse.
        """
        if refresh or self._defaults is None:
            api_defaults = self._api_client.backend_pulse_defaults(self.name())
            if api_defaults:
                decode_pulse_defaults(api_defaults)
                self._defaults = PulseDefaults.from_dict(api_defaults)
            else:
                self._defaults = None

        return self._defaults

    def job_limit(self) -> BackendJobLimit:
        """Return the job limit for the backend.

        The job limit information includes the current number of active jobs
        you have on the backend and the maximum number of active jobs you can have
        on it.

        Note:
            Job limit information for a backend is provider specific.
            For example, if you have access to the same backend via
            different providers, the job limit information might be
            different for each provider.

        If the method call was successful, you can inspect the job limit for
        the backend by accessing the ``maximum_jobs`` and ``active_jobs`` attributes
        of the :class:`BackendJobLimit<BackendJobLimit>` instance returned. For example::

            backend_job_limit = backend.job_limit()
            maximum_jobs = backend_job_limit.maximum_jobs
            active_jobs = backend_job_limit.active_jobs

        If ``maximum_jobs`` is equal to ``None``, then there is
        no limit to the maximum number of active jobs you could
        have on the backend.

        Returns:
            The job limit for the backend, with this provider.

        Raises:
            IBMBackendApiProtocolError: If an unexpected value is received from the server.
        """
        api_job_limit = self._api_client.backend_job_limit(self.name())

        try:
            job_limit = BackendJobLimit(**api_job_limit)
            if job_limit.maximum_jobs == -1:
                # Manually set `maximum` to `None` if backend has no job limit.
                job_limit.maximum_jobs = None
            return job_limit
        except TypeError as ex:
            raise IBMBackendApiProtocolError(
                'Unexpected return value received from the server when '
                'querying job limit data for the backend: {}.'.format(ex)) from ex

    def remaining_jobs_count(self) -> Optional[int]:
        """Return the number of remaining jobs that could be submitted to the backend.

        Note:
            The number of remaining jobs for a backend is provider
            specific. For example, if you have access to the same backend
            via different providers, the number of remaining jobs might
            be different for each. See :class:`BackendJobLimit<BackendJobLimit>`
            for the job limit information of a backend.

        If ``None`` is returned, there are no limits to the maximum
        number of active jobs you could have on the backend.

        Returns:
            The remaining number of jobs a user could submit to the backend, with
            this provider, before the maximum limit on active jobs is reached.

        Raises:
            IBMBackendApiProtocolError: If an unexpected value is received from the server.
        """
        job_limit = self.job_limit()

        if job_limit.maximum_jobs is None:
            return None

        return job_limit.maximum_jobs - job_limit.active_jobs

    def active_jobs(self, limit: int = 10) -> List[IBMJob]:
        """Return the unfinished jobs submitted to this backend.

        Return the jobs submitted to this backend, with this provider, that are
        currently in an unfinished job status state. The unfinished
        :class:`JobStatus<qiskit.providers.jobstatus.JobStatus>` states
        include: ``INITIALIZING``, ``VALIDATING``, ``QUEUED``, and ``RUNNING``.

        Args:
            limit: Number of jobs to retrieve.

        Returns:
            A list of the unfinished jobs for this backend on this provider.
        """
        # Get the list of api job statuses which are not a final api job status.
        active_job_states = list({api_status_to_job_status(status)
                                  for status in ApiJobStatus
                                  if status not in API_JOB_FINAL_STATES})
        provider = self.provider()
        return provider.backend.jobs(status=active_job_states, limit=limit)

    def reservations(
            self,
            start_datetime: Optional[python_datetime] = None,
            end_datetime: Optional[python_datetime] = None
    ) -> List[BackendReservation]:
        """Return backend reservations.

        If start_datetime and/or end_datetime is specified, reservations with
        time slots that overlap with the specified time window will be returned.

        Some of the reservation information is only available if you are the
        owner of the reservation.

        Args:
            start_datetime: Filter by the given start date/time, in local timezone.
            end_datetime: Filter by the given end date/time, in local timezone.

        Returns:
            A list of reservations that match the criteria.
        """
        start_datetime = local_to_utc(start_datetime) if start_datetime else None
        end_datetime = local_to_utc(end_datetime) if end_datetime else None
        raw_response = self._api_client.backend_reservations(
            self.name(), start_datetime, end_datetime)
        return convert_reservation_data(raw_response, self.name())

    def configuration(self) -> Union[QasmBackendConfiguration, PulseBackendConfiguration]:
        """Return the backend configuration.

        Backend configuration contains fixed information about the backend, such
        as its name, number of qubits, basis gates, coupling map, quantum volume, etc.

        The schema for backend configuration can be found in
        `Qiskit/ibm-quantum-schemas
        <https://github.com/Qiskit/ibm-quantum-schemas/blob/main/schemas/backend_configuration_schema.json>`_.

        Returns:
            The configuration for the backend.
        """
        return self._configuration

    def __repr__(self) -> str:
        credentials_info = ''
        if self.hub:
            credentials_info = "hub='{}', group='{}', project='{}'".format(
                self.hub, self.group, self.project)
        return "<{}('{}') from IBMProvider({})>".format(
            self.__class__.__name__, self.name(), credentials_info)

    def _deprecate_id_instruction(
            self,
            circuits: Union[QuantumCircuit, Schedule,
                            List[Union[QuantumCircuit, Schedule]]]
    ) -> None:
        """Raise a DeprecationWarning if any circuit contains an 'id' instruction.

        Additionally, if 'delay' is a 'supported_instruction', replace each 'id'
        instruction (in-place) with the equivalent ('sx'-length) 'delay' instruction.

        Args:
            circuits: The individual or list of :class:`~qiskit.circuits.QuantumCircuit` or
                :class:`~qiskit.pulse.Schedule` objects passed to
                :meth:`IBMBackend.run()<IBMBackend.run>`. Modified in-place.

        Returns:
            None
        """

        id_support = 'id' in getattr(self.configuration(), 'basis_gates', [])
        delay_support = 'delay' in getattr(self.configuration(), 'supported_instructions', [])

        if not delay_support:
            return

        if not isinstance(circuits, List):
            circuits = [circuits]

        circuit_has_id = any(instr.name == 'id'
                             for circuit in circuits
                             if isinstance(circuit, QuantumCircuit)
                             for instr, qargs, cargs in circuit.data)

        if not circuit_has_id:
            return

        if not self.id_warning_issued:
            if id_support and delay_support:
                warnings.warn("Support for the 'id' instruction has been deprecated "
                              "from IBM hardware backends. Any 'id' instructions "
                              "will be replaced with their equivalent 'delay' instruction. "
                              "Please use the 'delay' instruction instead.", DeprecationWarning,
                              stacklevel=4)
            else:
                warnings.warn("Support for the 'id' instruction has been removed "
                              "from IBM hardware backends. Any 'id' instructions "
                              "will be replaced with their equivalent 'delay' instruction. "
                              "Please use the 'delay' instruction instead.", DeprecationWarning,
                              stacklevel=4)

            self.id_warning_issued = True

        dt_in_s = self.configuration().dt

        for circuit in circuits:
            if isinstance(circuit, Schedule):
                continue

            for idx, (instr, qargs, cargs) in enumerate(circuit.data):
                if instr.name == 'id':

                    sx_duration = self.properties().gate_length('sx', qargs[0].index)
                    sx_duration_in_dt = duration_in_dt(sx_duration, dt_in_s)

                    delay_instr = Delay(sx_duration_in_dt)

                    circuit.data[idx] = (delay_instr, qargs, cargs)


class IBMSimulator(IBMBackend):
    """Backend class interfacing with an IBM Quantum simulator."""

    @classmethod
    def _default_options(cls) -> Options:
        """Default runtime options."""
        options = super()._default_options()
        options.update_options(noise_model=None, seed_simulator=None)
        return options

    def properties(
            self,
            refresh: bool = False,
            datetime: Optional[python_datetime] = None
    ) -> None:
        """Return ``None``, simulators do not have backend properties."""
        return None

    def run(    # type: ignore[override]
            self,
            circuits: Union[QuantumCircuit, Schedule,
                            List[Union[QuantumCircuit, Schedule]]],
            job_name: Optional[str] = None,
            job_tags: Optional[List[str]] = None,
            backend_options: Optional[Dict] = None,
            noise_model: Any = None,
            **kwargs: Dict
    ) -> IBMJob:
        """Run a Circuit asynchronously.

        Args:
            circuits: An individual or a
                list of :class:`~qiskit.circuits.QuantumCircuit` or
                :class:`~qiskit.pulse.Schedule` objects to run on the backend.
            job_name: Custom name to be assigned to the job. This job
                name can subsequently be used as a filter in the
                :meth:`jobs` method. Job names do not need to be unique.
            job_tags: Tags to be assigned to the jobs. The tags can subsequently be used
                as a filter in the
                :meth:`IBMBackendService.jobs()
                <qiskit_ibm.ibm_backend_service.IBMBackendService.jobs>` method.
            backend_options: DEPRECATED dictionary of backend options for the execution.
            noise_model: Noise model.
            kwargs: Additional runtime configuration options. They take
                precedence over options of the same names specified in `backend_options`.

        Returns:
            The job to be executed.
        """
        # pylint: disable=arguments-differ
        if backend_options is not None:
            warnings.warn("Use of `backend_options` is deprecated and will "
                          "be removed in a future release."
                          "You can now pass backend options as key-value pairs to the "
                          "run() method. For example: backend.run(circs, shots=2048).",
                          DeprecationWarning, stacklevel=2)
        backend_options = backend_options or {}
        run_config = copy.deepcopy(backend_options)
        if noise_model:
            try:
                noise_model = noise_model.to_dict()
            except AttributeError:
                pass
        run_config.update(kwargs)
        return super().run(circuits, job_name=job_name,
                           job_tags=job_tags,
                           noise_model=noise_model, **run_config)


class IBMRetiredBackend(IBMBackend):
    """Backend class interfacing with an IBM Quantum device no longer available."""

    def __init__(
            self,
            configuration: Union[QasmBackendConfiguration, PulseBackendConfiguration],
            provider: 'ibm_provider.IBMProvider',
            credentials: Credentials,
            api_client: AccountClient
    ) -> None:
        """IBMRetiredBackend constructor.

        Args:
            configuration: Backend configuration.
            provider: IBM Quantum account provider
            credentials: IBM Quantum credentials.
            api_client: IBM Quantum client used to communicate with the server.
        """
        super().__init__(configuration, provider, credentials, api_client)
        self._status = BackendStatus(
            backend_name=self.name(),
            backend_version=self.configuration().backend_version,
            operational=False,
            pending_jobs=0,
            status_msg='This backend is no longer available.')

    @classmethod
    def _default_options(cls) -> Options:
        """Default runtime options."""
        return Options()

    def properties(
            self,
            refresh: bool = False,
            datetime: Optional[python_datetime] = None
    ) -> None:
        """Return the backend properties."""
        return None

    def defaults(self, refresh: bool = False) -> None:
        """Return the pulse defaults for the backend."""
        return None

    def status(self) -> BackendStatus:
        """Return the backend status."""
        return self._status

    def job_limit(self) -> None:
        """Return the job limits for the backend."""
        return None

    def remaining_jobs_count(self) -> None:
        """Return the number of remaining jobs that could be submitted to the backend."""
        return None

    def active_jobs(self, limit: int = 10) -> None:
        """Return the unfinished jobs submitted to this backend."""
        return None

    def reservations(
            self,
            start_datetime: Optional[python_datetime] = None,
            end_datetime: Optional[python_datetime] = None
    ) -> List[BackendReservation]:
        return []

    def run(    # type: ignore[override]
            self,
            *args: Any,
            **kwargs: Any
    ) -> None:
        """Run a Circuit."""
        # pylint: disable=arguments-differ
        raise IBMBackendError('This backend ({}) is no longer available.'.format(self.name()))

    @classmethod
    def from_name(
            cls,
            backend_name: str,
            provider: 'ibm_provider.IBMProvider',
            credentials: Credentials,
            api: AccountClient
    ) -> 'IBMRetiredBackend':
        """Return a retired backend from its name."""
        configuration = QasmBackendConfiguration(
            backend_name=backend_name,
            backend_version='0.0.0',
            n_qubits=1,
            basis_gates=[],
            simulator=False,
            local=False,
            conditional=False,
            open_pulse=False,
            memory=False,
            max_shots=1,
            gates=[GateConfig(name='TODO', parameters=[], qasm_def='TODO')],
            coupling_map=[[0, 1]],
        )
        return cls(configuration, provider, credentials, api)
