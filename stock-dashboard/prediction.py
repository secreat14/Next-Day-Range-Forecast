"""Next-session range models and serialization of fitted decision trees."""

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor


FEATURES = ('range1', 'range5', 'range22')
MODEL_PARAMETERS = {
    'n_estimators': 200,
    'max_depth': 5,
    'min_samples_leaf': 20,
    'random_state': 42,
    'n_jobs': 2,
}
MIN_TRAINING_ROWS = 120
DISPLAY_TREES = 3


def range_features(high, low, previous_close):
    amplitude = 100 * (high - low) / previous_close
    return pd.DataFrame({
        'range1': amplitude,
        'range5': amplitude.rolling(5).mean(),
        'range22': amplitude.rolling(22).mean(),
    })


def fitted_forest(features, target):
    return RandomForestRegressor(**MODEL_PARAMETERS).fit(features, target)


def explain_trees(model, prediction_row=None):
    results = []
    for index, estimator in enumerate(model.estimators_[:DISPLAY_TREES], start=1):
        tree = estimator.tree_
        path = set() if prediction_row is None else set(estimator.decision_path(
            np.asarray(prediction_row, dtype=np.float32)
        ).indices)
        nodes = []
        for node in range(tree.node_count):
            left, right = int(tree.children_left[node]), int(tree.children_right[node])
            nodes.append({
                'id': node,
                'samples': int(tree.n_node_samples[node]),
                'value': round(float(tree.value[node, 0, 0]), 6),
                'active': node in path,
                **({
                    'feature': FEATURES[tree.feature[node]],
                    'threshold': round(float(tree.threshold[node]), 6),
                    'left': left,
                    'right': right,
                } if left != right else {}),
            })
        results.append({'index': index, 'nodes': nodes})
    return results


class NextSessionForecaster:
    def forecast(self, bars):
        frame = pd.DataFrame.from_records(
            [(row.session, row.high, row.low, row.close) for row in bars],
            columns=['session', 'high', 'low', 'close'],
        ).set_index('session')
        features = range_features(frame.high, frame.low, frame.close.shift())
        target = features.range1.shift(-1)
        usable = features.join(target.rename('target')).dropna()
        if len(usable) < MIN_TRAINING_ROWS:
            return None
        model = fitted_forest(usable[list(FEATURES)], usable.target)
        prediction_row = features.iloc[[-1]]
        prediction = model.predict(prediction_row)[0]
        return {
            'amplitude': float(prediction),
            'baseline': float(prediction_row.range5.iloc[0]),
            'features': {key: float(prediction_row[key].iloc[0]) for key in FEATURES},
            'trainingRows': len(usable),
            'trainingStart': usable.index[0],
            'trainingEnd': usable.index[-1],
            'estimators': len(model.estimators_),
            'trees': explain_trees(model, prediction_row),
        }
