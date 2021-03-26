import abc
import logging
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from itertools import product, zip_longest
from typing import Any, Dict, Generator, Generic, Iterable, Iterator, List, Optional, Tuple, Type, TypeVar, Union

import numpy as np
from log_calls import record_history
from pandas import DataFrame
from sklearn.model_selection import train_test_split

from lightautoml.addons.uplift import meta_learners as uplift_meta_learners
from lightautoml.addons.uplift import utils as uplift_utils
from lightautoml.addons.uplift.meta_learners import MetaLearner, TLearner, XLearner
from lightautoml.addons.uplift.metrics import calculate_uplift_auc
from lightautoml.automl.base import AutoML
from lightautoml.automl.presets.tabular_presets import TabularAutoML
from lightautoml.report.report_deco import ReportDecoUplift
from lightautoml.tasks import Task
from lightautoml.utils.logging import get_logger
from lightautoml.utils.timer import Timer


logger = get_logger(__name__)


T = TypeVar('T')

@record_history(enabled=False)
@dataclass
class Wrapper(Generic[T]):
    """Wrapper for class"""
    name: str
    klass: T
    params: Dict[str, Any]

    def __call__(self):
        def _init(x: Any):
            if issubclass(x.__class__, Wrapper):
                return x()
            elif isinstance(x, list):
                return [_init(elem) for elem in x]
            elif isinstance(x, dict):
                return {k: _init(v) for k,v in x.items()}
            else:
                return x

        init_params = {k: deepcopy(_init(v)) for k, v in self.params.items()}

        return self.klass(**init_params)

    def update_params(self, d: Dict[str, Any]):
        """Update parameters.

        Rewrite old value of key by new value.

        """
        self.params.update(d)


@record_history(enabled=False)
class BaseLearnerWrapper(Wrapper[Type[AutoML]]): pass


@record_history(enabled=False)
class MetaLearnerWrapper(Wrapper[Type[MetaLearner]]):
    def update_baselearner_params(self, d: Dict[str, Any]):
        new_params: Dict[str, Any] = {}
        for k, v in self.params.items():
            if isinstance(v, BaseLearnerWrapper):
                v_t = deepcopy(v)
                v.update_params(d)
                new_params[k] = v_t
            elif isinstance(v, List):
                vs_t = []
                for x in v:
                    if isinstance(x, BaseLearnerWrapper):
                        x.update_params(d)
                    vs_t.append(x)
                new_params[k] = vs_t
            else:
                new_params[k] = v

        self.params = new_params


@record_history(enabled=False)
class BaseAutoUplift(metaclass=abc.ABCMeta):
    def __init__(self,
                 base_task: Task,
                 metric: str = 'adj_qini',
                 normed_metric: bool = True,
                 increasing_metric: bool = True,
                 test_size: float = 0.2,
                 timeout: Optional[int] = None,
                 timeout_single_learner: Optional[int] = None,
                 random_state: int = 42):
        """
        Args:
            base_task: Task ('binary'/'reg') if there aren't candidates.
            metric: Uplift metric.
            normed_metric: Normalize or not uplift metric.
            increasing_metric: Increasing metric
            test_size: Size of test part, which use for.
            timeout: Global timeout of autouplift. Doesn't work when uplift_candidates is not default.
            timeout_single_learner: Timeout single baselearner, if not specified, it's selected automatically.
            random_state: Random state.

        """
        assert 0.0 < test_size < 1.0, "'test_size' must be between (0.0, 1.0)"

        self.base_task = base_task
        self.metric = metric
        self.normed_metric = normed_metric
        self.increasing_metric = increasing_metric
        self.test_size = test_size
        self.timeout = timeout
        self.timeout_single_learner = timeout_single_learner
        self.random_state = random_state

        self._timer = Timer()
        if timeout is not None:
            self._timer._timeout = timeout

    @abc.abstractmethod
    def fit(self, train_data: DataFrame, roles: Dict):
        pass

    @abc.abstractmethod
    def predict(self, data: Any) -> Tuple[np.ndarray, ...]:
        pass

    def _prepare_data(self, data: DataFrame, roles: dict) -> Tuple[DataFrame, DataFrame, np.ndarray, np.ndarray]:
        """Prepare data for training part.

        Args:
            train_data: Dataset to train.
            roles: Roles dict with 'treatment' roles.

        Returns:
            train_data: Train part of data
            test_data: Test part of data
            test_treatment: Treatment values of test data
            test_target: Target values of test data

        """
        target_role, target_col = uplift_utils._get_target_role(roles)
        treatment_role, treatment_col = uplift_utils._get_treatment_role(roles)

        stratify_value = data[target_col] + 10 * data[treatment_col]

        train_data, test_data = train_test_split(data, test_size=self.test_size, stratify=stratify_value, random_state=self.random_state)
        test_treatment = test_data[treatment_col].ravel()
        test_target = test_data[target_col].ravel()

        return train_data, test_data, test_treatment, test_target


@record_history(enabled=False)
class AutoUplift(BaseAutoUplift):
    """AutoUplift

    Using greed-search to choose best uplift-approach.

    Attributes:
        _tabular_timeout: Timeout for base learner in Tabularperset
        __THRESHOLD_DISBALANCE_TREATMENT__: Threshold for imbalance treatment.
            Condition: | treatment.mean() - 0.5| > __THRESHOLD_DISBALANCE_TREATMENT__

    """

    def __init__(self,
                 base_task: Task,
                 uplift_candidates: List[MetaLearnerWrapper] = [],
                 add_dd_candidates: bool = False,
                 metric: str = 'adj_qini',
                 normed_metric: bool = True,
                 increasing_metric: bool = True,
                 test_size: float = 0.2,
                 threshold_imbalance_treatment: float = 0.2,
                 timeout: Optional[int] = None,
                 timeout_single_learner: Optional[int] = None,
                 random_state: int = 42):
        """
        Args:
            base_task: Task ('binary'/'reg') if there aren't candidates.
            uplift_candidates: List of metalearners with params and custom name.
            add_dd_candidates: Add data depend candidates. Doesn't work when uplift_candidates is not default.
            metric: Uplift metric.
            normed_metric: Normalize or not uplift metric.
            increasing_metric: Increasing metric
            test_size: Size of test part, which use for.
            threshold_imbalance_treatment: Threshold for imbalance treatment.
                Condition: | MEAN(treatment) - 0.5| > threshold_imbalance_treatment
            timeout: Global timeout of autouplift. Doesn't work when uplift_candidates is not default.
            timeout_single_learner: Timeout single baselearner, if not specified, it's selected automatically.
            random_state: Random state.

        """
        super().__init__(base_task, metric, normed_metric, increasing_metric, test_size, timeout, timeout_single_learner, random_state)

        if len(uplift_candidates) > 0:
            if timeout is not None:
                logger.warning("'timeout' isn't used when 'uplift_candidates' is specified.")
            if add_dd_candidates:
                logger.warning("'add_dd_candidates' isn't used when 'uplift_candidates' is specified.")

        self.checkout_timeout = True
        if len(uplift_candidates) > 0:
            self.uplift_candidates = uplift_candidates
            self.checkout_timeout = False
        else:
            self.uplift_candidates = []

        self.best_metalearner: Optional[MetaLearner] = None
        self.best_metalearner_candidate: Optional[MetaLearnerWrapper] = None
        self.add_dd_candidates = add_dd_candidates
        self.candidate_holdout_metrics: List[Union[float, None]] = []
        self._threshold_imbalance_treatment = threshold_imbalance_treatment

    def fit(self, data: DataFrame, roles: Dict):
        """Fit AutoUplift.

        Choose best metalearner and fit it.

        Args:
            train_data: Dataset to train.
            roles: Roles dict with 'treatment' roles.

        """
        train_data, test_data, test_treatment, test_target = self._prepare_data(data, roles)

        best_metalearner: Optional[MetaLearner] = None
        best_metalearner_candidate_info: Optional[MetaLearnerWrapper] = None
        max_metric_value = 0.0

        if len(self.uplift_candidates) == 0:
            self._generate_uplift_candidates(data, roles)

        self.candidate_holdout_metrics = [None] * len(self.uplift_candidates)

        self._timer.start()

        for idx_candidate, candidate_info in enumerate(self.uplift_candidates):
            metalearner = candidate_info()
            metalearner.fit(train_data, roles)
            logger.info("Uplift candidate #{} [{}] is fitted".format(idx_candidate, candidate_info.name))

            uplift_pred, _, _ = metalearner.predict(test_data)

            metric_value = calculate_uplift_auc(test_target, uplift_pred.ravel(), test_treatment, self.metric,
                self.normed_metric)
            self.candidate_holdout_metrics[idx_candidate] = metric_value

            if best_metalearner_candidate_info is None:
                best_metalearner = metalearner
                best_metalearner_candidate_info = candidate_info
            elif max_metric_value < metric_value:
                best_metalearner = metalearner
                best_metalearner_candidate_info = candidate_info
                max_metric_value = metric_value

            if self._timer.time_limit_exceeded():
                logger.warning("Time of training exceeds 'timeout': {} > {}.".format(self._timer.time_spent, self.timeout))
                logger.warning("There is fitted {}/{} candidates".format(idx_candidate + 1, len(self.uplift_candidates)))
                if idx_candidate + 1 < len(self.uplift_candidates):
                    logger.warning("Try to increase 'timeout' or set 'None'(eq. infinity)")
                break

        self.best_metalearner_candidate_info = best_metalearner_candidate_info
        self.best_metalearner = best_metalearner

    def predict(self, data: DataFrame) -> Tuple[np.ndarray, ...]:
        """Predict treatment effects

        Predict treatment effects using best metalearner

        Args:
            data: Dataset to perform inference.

        Returns:
            treatment_effect: Predictions of treatment effects
            ...: None or predictions of base task values on treated(control)-stage

        """
        assert self.best_metalearner is not None, "First call 'self.fit(...)', to choose best metalearner"

        return self.best_metalearner.predict(data)

    def create_best_meta_learner(self, need_report: bool = True, update_metalearner_params: Dict[str, Any] = {})\
            -> Union[MetaLearner, ReportDecoUplift]:
        """ Create 'raw' best metalearner with(without) report functionality.

        Returned metalearner should be refitted.

        Args:
            need_report: Wrap best metalearner into Report
            update_metalearner_params: Parameters inner learner.
                Recommended using - increasing timeout of 'TabularAutoML' learner for better scores.
                Example: {'timeout': None}.

        Returns:
            metalearner_deco: Best metalearner is wrapped or not by ReportDecoUplift.

        """
        assert self.best_metalearner_candidate_info is not None, "First call 'self.fit(...), to choose best metalearner"

        candidate_info = deepcopy(self.best_metalearner_candidate_info)
        if update_metalearner_params:
            candidate_info.update_baselearner_params(update_metalearner_params)

        best_metalearner = candidate_info()

        if need_report:
            rdu = ReportDecoUplift()
            best_metalearner = rdu(best_metalearner)

        return best_metalearner

    def get_metalearners_ranting(self) -> DataFrame:
        """Get rating of metalearners.

        Returns:
            rating_table: DataFrame with rating.

        """
        rating_table = DataFrame({
            'MetaLearner': [info.name for info in self.uplift_candidates],
            'Parameters': [info.params for info in self.uplift_candidates],
            'Metrics': self.candidate_holdout_metrics
        })

        rating_table['Rank'] = rating_table['Metrics'].rank(method='first', ascending=False)
        rating_table.sort_values('Rank', inplace=True)
        rating_table.reset_index(drop=True, inplace=True)

        return rating_table

    def _generate_uplift_candidates(self, data: DataFrame, roles):
        """Generate uplift candidates.

        Combine uplift candidates from 'default' and 'data-depends' candidates.

        Args:
            train_data: Dataset to train.
            roles: Roles dict with 'treatment' roles.

        Returns:
            candidates: List of uplift candidates.

        """
        # Number TabularAutoML in all posible uplift candidates
        num_tabular_automls = 16 if self.add_dd_candidates else 11

        if self.timeout_single_learner is not None:
            self._tabular_timeout = self.timeout_single_learner
        else:
            self._tabular_timeout = self.timeout if self.timeout is None else int(self.timeout / num_tabular_automls)

        self.uplift_candidates = self._default_uplift_candidates

        if self.add_dd_candidates:
            self.uplift_candidates.extend(
                self._generate_data_depend_uplift_candidates(data, roles)
            )

    @property
    def _default_uplift_candidates(self) -> List[MetaLearnerWrapper]:
        """Default uplift candidates"""
        return [
            MetaLearnerWrapper(
                name='__TLearner__Default__',
                klass=TLearner,
                params={'base_task': self.base_task}
            ),
            MetaLearnerWrapper(
                name='__XLearner__Default__',
                klass=XLearner,
                params={'base_task': self.base_task}
            ),
            MetaLearnerWrapper(
                name='__TLearner__TabularAutoML__',
                klass=TLearner,
                params={
                    'treatment_learner': BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': self.base_task, 'timeout': self._tabular_timeout}
                    ),
                    'control_learner': BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': self.base_task, 'timeout': self._tabular_timeout}
                    )
                }
            ),
            MetaLearnerWrapper(
                name='__XLearner__Propensity_Linear__Other_TabularAutoML__',
                klass=XLearner,
                params = {
                    'outcome_learners': [BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': self.base_task, 'timeout': self._tabular_timeout})],
                    'effect_learners': [BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': Task('reg'), 'timeout': self._tabular_timeout})],
                    'propensity_learner': BaseLearnerWrapper(
                        name='__Linear__',
                        klass=uplift_utils.create_linear_automl,
                        params={'task': Task('binary')})
                }
            ),
            MetaLearnerWrapper(
                name='__XLearner__TabularAutoML__',
                klass=XLearner,
                params={
                    'outcome_learners': [BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': self.base_task, 'timeout': self._tabular_timeout})],
                    'effect_learners': [BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': Task('reg'), 'timeout': self._tabular_timeout})],
                    'propensity_learner': BaseLearnerWrapper(
                        name='__TabularAutoML__',
                        klass=TabularAutoML,
                        params={'task': Task('binary'), 'timeout': self._tabular_timeout}),
                }
            )
        ]

    def _generate_data_depend_uplift_candidates(self, data: DataFrame, roles: dict) -> List[MetaLearnerWrapper]:
        """Generate uplift candidates.

        Generate new uplift candidates which depend from data.

        If there is imbalance in treatment , adds the simple linear model for smaller stage.

        Args:
            train_data: Dataset to train.
            roles: Roles dict with 'treatment' roles.

        Returns:
            candidates: List of new uplift candidates.

        """
        dd_uplift_candidates: List[MetaLearnerWrapper] = []

        _, treatment_col = uplift_utils._get_treatment_role(roles)

        treatment_rate = data[treatment_col].mean()

        is_imbalance_treatment = False
        if treatment_rate > 0.5 + self._threshold_imbalance_treatment:
            is_imbalance_treatment = True
            ordered_outcome_learners = [
                BaseLearnerWrapper(
                    name='__Linear__',
                    klass=uplift_utils.create_linear_automl,
                    params={'task': Task('binary')}),
                BaseLearnerWrapper(
                    name='__TabularAutoML__',
                    klass=TabularAutoML,
                    params={'task': self.base_task, 'timeout': self._tabular_timeout})
            ]
            ordered_effect_learners = [
                BaseLearnerWrapper(
                    name='__Linear__',
                    klass=uplift_utils.create_linear_automl,
                    params={'task': Task('reg')}),
                BaseLearnerWrapper(
                    name='__TabularAutoML__',
                    klass=TabularAutoML,
                    params={'task': Task('reg'), 'timeout': self._tabular_timeout})
            ]
            control_model, treatment_model = 'Linear', 'Preset'
        elif treatment_rate < 0.5 - self._threshold_imbalance_treatment:
            is_imbalance_treatment = True
            ordered_outcome_learners = [
                BaseLearnerWrapper(
                    name='__TabularAutoML__',
                    klass=TabularAutoML,
                    params={'task': self.base_task, 'timeout': self._tabular_timeout}),
                uplift_utils.create_linear_automl(task=Task('binary'))
            ]
            ordered_effect_learners = [
                uplift_utils.create_linear_automl(task=Task('reg')),
                BaseLearnerWrapper(
                    name='__TabularAutoML__',
                    klass=TabularAutoML,
                    params={'task': Task('reg'), 'timeout': self._tabular_timeout})
            ]
            control_model, treatment_model = 'Preset', 'Linear'

        if is_imbalance_treatment:
            dd_uplift_candidates.extend([
                MetaLearnerWrapper(
                    name='XLearner__Propensity_Linear__Control_{}__Treatment_{}'.format(control_model, treatment_model),
                    klass=XLearner,
                    params={
                        'outcome_learners': ordered_outcome_learners,
                        'effect_learners':  ordered_effect_learners,
                        'propensity_learner': BaseLearnerWrapper(
                            name='__Linear__',
                            klass=uplift_utils.create_linear_automl,
                            params={'task': Task('binary')})
                    }
                ),
                MetaLearnerWrapper(
                    name='XLearner__Control_{}__Treatment_{}'.format(control_model, treatment_model),
                    klass=XLearner,
                    params={
                        'outcome_learners': ordered_outcome_learners,
                        'effect_learners':  ordered_effect_learners,
                        'propensity_learner': BaseLearnerWrapper(
                            name='__TabularAutoML__',
                            klass=TabularAutoML,
                            params={'task': Task('binary'), 'timeout': self._tabular_timeout})
                    }
                )
            ])

        return dd_uplift_candidates


MLStageFullName = Tuple[str, ...]
TrainedMetaLearnerFullName = Tuple[str, Tuple[Tuple[MLStageFullName, str], ...]]

@record_history(enabled=False)
@dataclass
class MetaLearnerStage():
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    prev_stage: Optional['MetaLearnerStage'] = None

    def full_name(self) -> MLStageFullName:
        fn: MLStageFullName
        if self.prev_stage is None:
            fn = (self.name,)
        else:
            fn = (*self.prev_stage.full_name(), self.name)

        return fn


@record_history(enabled=False)
@dataclass
class TrainedStageBaseLearner:
    stage_bl: BaseLearnerWrapper
    prev_stage_bl: Optional[BaseLearnerWrapper]
    trained_model: AutoML
    pred: np.ndarray


@record_history(enabled=False)
class AutoUpliftTX(BaseAutoUplift):
    """AutoUplift for T(X)-Learners.

    Optimizes the selection of best metalearner between TLearner and XLearner,
    without don't retrain the baselearners of the common parts. TLearner is the first half of XLearner.

    """

    __MAP_META_TO_STAGES__: Dict[str, List[MetaLearnerStage]] = {
        'TLearner': [
            MetaLearnerStage(name='outcome_control'),
            MetaLearnerStage(name='outcome_treatment')
        ],
        'XLearner': [
            MetaLearnerStage(name='outcome_control'),
            MetaLearnerStage(name='outcome_treatment'),
            MetaLearnerStage(name='propensity', params={'task': Task('binary')}),
            MetaLearnerStage(name='effect_control', params={'task': Task('reg')}, prev_stage=MetaLearnerStage(name='outcome_treatment')),
            MetaLearnerStage(name='effect_treatment', params={'task': Task('reg')}, prev_stage=MetaLearnerStage(name='outcome_control'))
        ]
    }

    def __init__(self,
                 base_task: Task,
                 baselearners: Optional[Union[List[BaseLearnerWrapper], Dict[MLStageFullName, List[BaseLearnerWrapper]]]] = None,
                 metalearners: List[str] = [],
                 metric: str = 'adj_qini',
                 normed_metric: bool = True,
                 increasing_metric: bool = True,
                 test_size: float = 0.2,
                 timeout: Optional[int] = None,
                 timeout_single_learner: Optional[int] = None,
                 random_state: int = 42):
        """
        Args:
            base_task: Task ('binary'/'reg') if there aren't candidates.
            baselearners: List of baselearners or baselearner divided into the groups (Dict).
            metalearners: List of metalearners.
            metric: Uplift metric.
            normed_metric: Normalize or not uplift metric.
            increasing_metric: Increasing metric.
            test_size: Size of test part, which use for.
            timeout: Global timeout of autouplift. Doesn't work when uplift_candidates is not default.
            timeout_single_learner: Timeout single baselearner, if not specified, it's selected automatically.
            random_state: Random state.

        """
        assert all(ml in self.__MAP_META_TO_STAGES__ for ml in metalearners), 'Currently available for {}.'.format(self.__MAP_META_TO_STAGES__)

        super().__init__(base_task, metric, normed_metric, increasing_metric, test_size, timeout, timeout_single_learner, random_state)

        self.baselearners = baselearners
        self.metalearners = metalearners if len(metalearners) > 0 else list(self.__MAP_META_TO_STAGES__)

        self._best_metalearner: MetaLearner
        self._best_metalearner_wrap: MetaLearnerWrapper

        self._trained_stage_baselearners: Dict[MLStageFullName, List[TrainedStageBaseLearner]] = defaultdict(list)
        self._metalearner_metrics: Dict[TrainedMetaLearnerFullName, float] = {}

        self._n_run_l2 = 3

    def fit(self, data: DataFrame, roles: dict):
        """Fit AutoUplift.

        Choose best metalearner and fit it.

        Args:
            train_data: Dataset to train.
            roles: Roles dict with 'treatment' roles.

        """
        train_data, test_data, test_treatment, test_target = self._prepare_data(data, roles)

        self._timer.start()

        for stage_info in self._generate_stage_baselearner_candidates():
            self._evaluate(stage_info, train_data, test_data, roles)

            if self._timer.time_limit_exceeded():
                logger.warning("Time of training exceeds 'timeout': {} > {}.".format(self._timer.time_spent, self.timeout))
                break

        self._calculate_ml_metric(test_treatment, test_target)

        self._set_best_metalearner()

    def predict(self, data: DataFrame) -> Tuple[np.ndarray, ...]:
        """Predict treatment effects

        Predict treatment effects using best metalearner

        Args:
            data: Dataset to perform inference.

        Returns:
            treatment_effect: Predictions of treatment effects
            ...: None or predictions of base task values on treated(control)-group

        """
        assert self._best_metalearner is not None, "First call 'self.fit(...)', to choose best metalearner."

        return self._best_metalearner.predict(data)

    def create_best_metalearner(self, need_report: bool = True, update_metalearner_params: Dict[str, Any] = {})\
            -> Union[MetaLearner, ReportDecoUplift]:
        """ Create 'raw' best metalearner with(without) report functionality.

        Returned metalearner should be refitted.

        Args:
            need_report: Wrap best metalearner into Report
            update_metalearner_params: Parameters inner learner.
                Recommended using - increasing timeout of 'TabularAutoML' learner for better scores.
                Example: {'timeout': None}.

        Returns:
            metalearner_deco: Best metalearner is wrapped or not by ReportDecoUplift.

        """
        assert len(self._trained_stage_baselearners) > 0, "First call 'self.fit(...), to choose best metalearner."

        ml_wrap = deepcopy(self._best_metalearner_wrap)
        if update_metalearner_params:
            ml_wrap.update_baselearner_params(update_metalearner_params)

        best_metalearner_raw = ml_wrap()

        if need_report:
            rdu = ReportDecoUplift()
            best_metalearner = rdu(best_metalearner_raw)

        return best_metalearner

    def get_metalearners_ranting(self) -> DataFrame:
        """Get rating of metalearners.

        Returns:
            rating_table: DataFrame with rating.

        """
        metalearner_names, params, metrics = [], [], []
        for ml_name, metric in self._metalearner_metrics.items():
            metalearner_names.append(ml_name[0])
            params.append(ml_name[1])
            metrics.append(metric)

        rating_table = DataFrame({
            'MetaLearner': metalearner_names,
            'Parameters': params,
            'Metrics': metrics,
        })

        rating_table['Rank'] = rating_table['Metrics'].rank(method='first', ascending=False)
        rating_table.sort_values('Rank', inplace=True)
        rating_table.reset_index(drop=True, inplace=True)

        return rating_table

    def _generate_stage_baselearner_candidates(self) -> Generator[Tuple[Tuple[MetaLearnerStage, BaseLearnerWrapper], ...], None, None]:#-> Generator[Tuple[str, BaseLearnerWrapper], None, None]:
        """Iterate through a stage of baselearners one at a time."""
        stage_baselearners = self._set_stage_baselearners()

        stage_by_levels = defaultdict(list)
        for stage in stage_baselearners:
            stage_by_levels[len(stage)].append(stage)

        pool_iter_levels = defaultdict(list)

        #TODO - WARNING! Work only with two levels.
        bls_level_1 = zip_longest(*(deepcopy(bls) for stage, bls in stage_baselearners.items() if stage in stage_by_levels[1]))
        first_run = True
        n_runs = 0
        for bls in bls_level_1:
            for stage_name_l1, bl_l1 in zip(stage_by_levels[1], bls):
                stage_l1 = self._extract_stage(stage_name_l1)

                for stage_name_l2 in stage_by_levels[2]:
                    if stage_name_l2[0:1] == stage_name_l1:
                        pool_iter_levels[2].append((stage_name_l2, bl_l1, iter(deepcopy(stage_baselearners[stage_name_l2]))))

                n_runs += 1
                yield ((stage_l1, bl_l1),)

                if not first_run:
                    n_stage_iters = len(pool_iter_levels[2])
                    if n_stage_iters == 0:
                        continue

                    for _ in range(min(n_stage_iters, self._n_run_l2)):
                        n_stage_iters = len(pool_iter_levels[2])
                        idx = np.random.randint(0, n_stage_iters, 1)[0]
                        try:
                            stage_name_l2, bl_l1, bls_iter = pool_iter_levels[2][idx]
                            bl_l2 = next(bls_iter)

                            stage_l1 = self._extract_stage(stage_name_l2[0:1])
                            stage_l2 = self._extract_stage(stage_name_l2)

                            n_runs += 1
                            yield ((stage_l1, bl_l1), (stage_l2, bl_l2))

                        except StopIteration:
                            pool_iter_levels[2].pop(idx)

                if n_runs >= len(stage_by_levels[1]):
                    first_run = False

        while len(pool_iter_levels[2]) > 0:
            n_stage_iters = len(pool_iter_levels[2])
            try:
                idx = np.random.randint(0, n_stage_iters, 1)[0]

                stage_name_l2, bl_l1, bls_iter = pool_iter_levels[2][idx]
                bl_l2 = next(bls_iter)

                stage_l1 = self._extract_stage(stage_name_l2[0:1])
                stage_l2 = self._extract_stage(stage_name_l2)

                yield ((stage_l1, bl_l1), (stage_l2, bl_l2))
            except StopIteration:
                pool_iter_levels[2].pop(idx)

    def _extract_stages(self) -> Generator[Tuple[str, MetaLearnerStage], None, None]:
        """Iterate over stages."""
        for ml_name, ml_stages in self.__MAP_META_TO_STAGES__.items():
            for stage in ml_stages:
                yield ml_name, stage

    def _extract_stage(self, full_name: MLStageFullName) -> MetaLearnerStage:
        """Return the first stage with a specific name."""
        for ml_name, stage in self._extract_stages():
            if stage.full_name() == full_name:
                return stage
        raise Exception("Can't find stage {}".format(full_name))

    def _set_stage_baselearners(self) -> Dict[MLStageFullName, List[BaseLearnerWrapper]]:
        """Generate baselearner for metalearners' stages.

        Returns:
            stage_baselearners: Stages with baselearners.

        """
        #TODO Timeout!
        baselearners = []
        if isinstance(self.baselearners, list):
            baselearners = deepcopy(self.baselearners)

        stage_baselearners = {}
        if isinstance(self.baselearners, dict):
            stage_baselearners = self.baselearners

        all_stages_full_names = set(stage.full_name() for ml, ml_stages in self.__MAP_META_TO_STAGES__.items() for stage in ml_stages)

        if len(stage_baselearners) != len(all_stages_full_names):
            timeout = self._calculate_single_bl_timeout(len(stage_baselearners) > 0)

            baselearners = deepcopy(self.__default_learners(tab_params={'timeout': timeout}))
            remain_stages_full_names = all_stages_full_names - set(stage_baselearners)

            #TODO: Consider the parameters of stages, not only names.
            bin_baselearners: List[BaseLearnerWrapper] = []
            reg_baselearners: List[BaseLearnerWrapper] = []
            raw_baselearners: List[BaseLearnerWrapper] = []

            for bl in baselearners:
                if 'task' in bl.params:
                    if bl.params['task'].name == 'binary':
                        bin_baselearners.append(bl)
                    if bl.params['task'].name == 'reg':
                        bin_baselearners.append(bl)
                else:
                    raw_baselearners.append(bl)

            for full_name in remain_stages_full_names:
                stage = self._extract_stage(full_name)

                stage_task = stage.params['task'] if 'task' in stage.params else self.base_task

                filled_baselearners = deepcopy(raw_baselearners)
                for idx in range(len(filled_baselearners)):
                    filled_baselearners[idx].params['task'] = stage_task

                baselearners_on_stage = filled_baselearners
                if stage_task.name == 'binary':
                    baselearners_on_stage.extend(bin_baselearners)
                elif stage_task.name == 'reg':
                    baselearners_on_stage.extend(bin_baselearners)

                stage_baselearners[full_name] = baselearners_on_stage

        return stage_baselearners

    def _calculate_single_bl_timeout(self, specify_stages: bool) -> Optional[int]:
        """Calculate timeout for single TabularAutoML from default baselearners.

        Returns:
            timeout: Timeout of TabularAutoML baselearner.

        """
        timeout: Optional[int] = None
        if not specify_stages:
            if self.timeout is not None:
                timeout = int(self.timeout / (2 * ('TLearner' in self.metalearners) + 5 * ('XLearner' in self.metalearners)))
            elif self.timeout_single_learner is not None:
                timeout = self.timeout_single_learner
        else:
            timeout = self.timeout_single_learner

        return timeout

    def _evaluate(self, stage_info: Tuple[Tuple[MetaLearnerStage, BaseLearnerWrapper], ...], train: DataFrame, test: DataFrame, roles: dict):
        """Evaluate baselearner: fit-train/predict-test.

        Args:
            stage_info: Full stage with baselearners names.

        """

        train_data, train_roles = self._prepare_data_for_stage(stage_info, train, roles)

        ml_stage, bl_wrap = stage_info[-1]
        prev_stage, prev_bl_wrap = None, None
        if len(stage_info) == 2:
            prev_stage, prev_bl_wrap = stage_info[0]

        bl = bl_wrap()

        bl.fit_predict(train_data, train_roles)
        test_pred = bl.predict(test).data.ravel()

        tsbl = TrainedStageBaseLearner(stage_bl=bl_wrap, prev_stage_bl=prev_bl_wrap, trained_model=bl, pred=test_pred)
        self._trained_stage_baselearners[ml_stage.full_name()].append(tsbl)

    def _prepare_data_for_stage(self, stage_info: Tuple[Tuple[MetaLearnerStage, BaseLearnerWrapper], ...], train: DataFrame, roles: dict) -> Tuple[DataFrame, Dict] :
        """Prepare data and roles for one metalearner's stage training.

        Args:
            stage_info: Stage info.
            train: Full train dataset.
            roles: Roles.

        """
        treatment_role, treatment_col = uplift_utils._get_treatment_role(roles)
        target_role, target_col = uplift_utils._get_target_role(roles)

        stage_name = stage_info[-1][0].name

        if len(stage_info) == 1:
            if stage_name == 'propensity':
                train_roles = deepcopy(roles)
                train_roles.pop(treatment_role)
                train_roles.pop(target_role)
                train_roles['target'] = treatment_col
                train_data = train.drop(target_col, axis=1)
            elif stage_name == 'outcome_control':
                train_roles = deepcopy(roles)
                train_roles.pop(treatment_role)
                train_data = train[train[treatment_col] == 0].drop(treatment_col, axis=1)
            elif stage_name == 'outcome_treatment':
                train_roles = deepcopy(roles)
                train_roles.pop(treatment_role)
                train_data = train[train[treatment_col] == 1].drop(treatment_col, axis=1)
            else:
                raise Exception('Wrong l1 stage name')
        elif len(stage_info) == 2:
            train_roles = deepcopy(roles)
            train_roles.pop(treatment_role)

            prev_ml_stage, prev_bl_wrap = stage_info[0]

            prev_stage_bl = [bl.trained_model for bl in self._trained_stage_baselearners[prev_ml_stage.full_name()] if bl.stage_bl.name == prev_bl_wrap.name][0]

            if stage_name == 'effect_control':
                train_data = train[train[treatment_col] == 0].drop(treatment_col, axis=1)
                opposite_gr_pred = prev_stage_bl.predict(train_data).data.ravel()

                train_data[target_col] = opposite_gr_pred - train_data[target_col]
            elif stage_name == 'effect_treatment':
                train_data = train[train[treatment_col] == 1].drop(treatment_col, axis=1)
                opposite_gr_pred = prev_stage_bl.predict(train_data).data.ravel()

                train_data[target_col] = train_data[target_col] - opposite_gr_pred
            else:
                raise Exception('Wrong l2 stage name')

        return train_data, train_roles

    def _calculate_ml_metric(self, test_target: np.ndarray, test_treatment: np.ndarray):
        """Calculate metalearners' metric."""
        for set_ml_stage_bls in self._bl_for_ml():
            for ml_name, stage_bls in set_ml_stage_bls.items():
                sbls = tuple(sorted([(stage_name, bl.stage_bl.name) for stage_name, bl in stage_bls.items()]))

                trained_ml_full_name = (ml_name, sbls)

                uplift_pred = self._metalearner_predict(ml_name, stage_bls)
                metric_value = calculate_uplift_auc(test_target, uplift_pred, test_treatment, self.metric, self.normed_metric)
                self._metalearner_metrics[trained_ml_full_name] = metric_value

    def _bl_for_ml(self) -> Generator[Dict[str, Dict[MLStageFullName, TrainedStageBaseLearner]], None, None]:
        """Prepare stage-baselearners for calculating metalearner metrics."""
        ready_metalearners = []
        for ml_name, ml_stages in self.__MAP_META_TO_STAGES__.items():
            ready_metalearners.append(all(s.full_name() in self._trained_stage_baselearners for s in ml_stages))

        if not any(ready_metalearners):
            raise Exception("No one metalearner can predict.")

        stage_baselearners = {}
        for stage_fullname, bls in self._trained_stage_baselearners.items():
            stage_baselearners[stage_fullname] = bls

        stage_names = list(stage_baselearners.keys())
        bls_prd = product(*(bls for _, bls in stage_baselearners.items()))

        for bl in bls_prd:
            set_bls = dict(zip(stage_names, bl))

            set_ml_with_sbls = {}
            for ml_name, ml_stages in self.__MAP_META_TO_STAGES__.items():
                ml_bls = {}
                for ml_stage in ml_stages:
                    ml_stage_full_name = ml_stage.full_name()
                    if ml_stage_full_name in set_bls:
                        trained_sbl = set_bls[ml_stage_full_name]
                        if trained_sbl.prev_stage_bl is None:
                            ml_bls[ml_stage_full_name] = trained_sbl
                        else:
                            if not ml_stage_full_name[0:1] in set_bls:
                                continue

                            if trained_sbl.prev_stage_bl.name == set_bls[ml_stage_full_name[0:1]].stage_bl.name:
                                ml_bls[ml_stage_full_name] = set_bls[ml_stage_full_name]

                if len(ml_bls) == len(ml_stages):
                    set_ml_with_sbls[ml_name] = ml_bls

            yield set_ml_with_sbls

    def _metalearner_predict(self, metalearner: str, baselearners: Dict[MLStageFullName, TrainedStageBaseLearner]) -> np.ndarray:
        """Metalearners prediction.

        Use pre-calculated scores of baselearners.

        Args:
            metalearner: Metalearner name.
            baselearners: Mapping metalearner stage name to trained baselearner.

        Returns:
            uplift_pred: Prediction.

        """
        if metalearner == 'TLearner':
            control_pred = baselearners[('outcome_control',)].pred
            treatment_pred = baselearners[('outcome_treatment',)].pred
            uplift_pred = treatment_pred - control_pred
        elif metalearner == 'XLearner':
            control_pred = baselearners[('outcome_treatment', 'effect_control')].pred
            treatment_pred = baselearners[('outcome_control', 'effect_treatment')].pred
            propensity_pred = baselearners[('propensity',)].pred
            uplift_pred = propensity_pred * treatment_pred + (1 - propensity_pred) * control_pred
        else:
            raise Exception()

        return uplift_pred.ravel()

    def _set_best_metalearner(self):
        """Select the best metalearner from the trained ones."""
        best_metric_value = None
        best_candidate = None
        for k, v in self._metalearner_metrics.items():
            if best_metric_value is None:
                best_candidate = k
                best_metric_value = v
            elif (self.increasing_metric and best_metric_value < v) or\
                    (not self.increasing_metric and best_metric_value > v):
                best_candidate = k
                best_metric_value = v

        ml_name, stages_params = best_candidate
        stages_params = dict(stages_params)

        self._best_metalearner = self._init_metalearner(ml_name, stages_params)
        self._best_metalearner_wrap = self._create_metalearner_wrap(ml_name, stages_params)

    def _init_metalearner(self, metalearner_name: str, bls: Dict[MLStageFullName, str]) -> MetaLearner:
        """Initialize best metalearner from trained baselearners.

        Args:
            metalearner_name: Metalearner name.
            bls: Mapping metalearner stage to baselearner name.

        Returns:
            ml: Metalearner.

        """
        ml: Optional[MetaLearner] = None
        if metalearner_name == 'TLearner':
            ocl = self._get_trained_bl(('outcome_control',), bls[('outcome_control',)]).trained_model
            otl = self._get_trained_bl(('outcome_treatment',), bls[('outcome_treatment',)]).trained_model

            ml = TLearner(
                control_learner=ocl,
                treatment_learner=otl
            )
        elif metalearner_name == 'XLearner':
            ocl = self._get_trained_bl(('outcome_control',), bls[('outcome_control',)]).trained_model
            otl = self._get_trained_bl(('outcome_treatment',), bls[('outcome_treatment',)]).trained_model
            pl =  self._get_trained_bl(('propensity',), bls[('propensity',)]).trained_model
            ecl = self._get_trained_bl(('outcome_treatment', 'effect_control'), bls[('outcome_treatment', 'effect_control')]).trained_model
            etl = self._get_trained_bl(('outcome_control', 'effect_treatment'), bls[('outcome_control', 'effect_treatment')]).trained_model

            ml = XLearner(
                outcome_learners=[ocl, otl],
                effect_learners=[ecl, etl],
                propensity_learner=pl
            )
        else:
            raise Exception()

        return ml

    def _get_trained_bl(self, metalearner_stage: MLStageFullName, baselearner_name: str):
        for bl in self._trained_stage_baselearners[metalearner_stage]:
            if bl.stage_bl.name == baselearner_name:
                return bl
        raise Exception("There isn't baselearner {}".format(baselearner_name))

    def _create_metalearner_wrap(self, metalearner_name: str, bls: Dict[MLStageFullName, str]) -> MetaLearnerWrapper:
        """Create of best metalearner wrapper.

        Args:
            metalearner_name: Name.
            bls: Mapping metalearner stage to baselearner name.

        Returns:
            ml_wrap: Best metalearner wrap.

        """
        ml_wrap_name = '__ML__{ML}'.format(ML=metalearner_name)

        ml_wrap: Optional[MetaLearnerWrapper] = None
        if metalearner_name == 'TLearner':
            ocl = self._get_trained_bl(('outcome_control',), bls[('outcome_control',)]).stage_bl
            otl = self._get_trained_bl(('outcome_treatment',), bls[('outcome_treatment',)]).stage_bl

            ml_wrap = MetaLearnerWrapper(
                name=ml_wrap_name,
                klass=TLearner,
                params={'control_learner': ocl, 'treatment_learner': otl}
            )
        elif metalearner_name == 'XLearner':
            ocl = self._get_trained_bl(('outcome_control',), bls[('outcome_control',)]).stage_bl
            otl = self._get_trained_bl(('outcome_treatment',), bls[('outcome_treatment',)]).stage_bl
            pl =  self._get_trained_bl(('propensity',), bls[('propensity',)]).stage_bl
            ecl = self._get_trained_bl(('outcome_treatment', 'effect_control'), bls[('outcome_treatment', 'effect_control')]).stage_bl
            etl = self._get_trained_bl(('outcome_control', 'effect_treatment'), bls[('outcome_control', 'effect_treatment')]).stage_bl

            ml_wrap = MetaLearnerWrapper(
                name=ml_wrap_name,
                klass=XLearner,
                params={
                    'outcome_learners': [ocl, otl],
                    'effect_learners': [ecl, etl],
                    'propensity_learner': pl
                }
            )
        else:
            raise Exception()

        return ml_wrap

    def __default_learners(self, lin_params: Dict[str, Any] = {},
                           tab_params: Dict[str, Any] = {'timeout': None} ) -> List[BaseLearnerWrapper]:
        """Predefined baselearners.

        Returns:
            baselearners: Default.

        """
        return [
            BaseLearnerWrapper(
                name='__Linear__',
                klass=uplift_utils.create_linear_automl,
                params=lin_params,
            ),
            BaseLearnerWrapper(
                name='__Tabular__',
                klass=TabularAutoML,
                params=tab_params
            )
        ]