"""A quadratic on the inputs a strategy already tried, solved for a stationary point.

The height is the result curve. Enter the tried attempts, fit

    height = c + b·z + zᵀ A z

on the standardized inputs, then solve the gradient equal to zero.
A stationary point is worth a backtest only when it is a maximum inside the
range that was already tried and it scores above the best attempt on the table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from futuresfund.objective import result_curve


@dataclass(frozen=True)
class QuadraticFit:
    """The fitted surface and the point where its slope is zero."""

    inputs: tuple[str, ...]
    count: int
    use_log: bool
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    system: tuple[tuple[float, ...], ...]
    critical_z: tuple[float, ...]
    critical_inputs: dict[str, float]
    predicted: float
    eigenvalues: tuple[float, ...]
    kind: str
    inside_tried_range: bool
    tried_min: dict[str, float]
    tried_max: dict[str, float]
    r2: float

    def predict(self, values: dict) -> float:
        """Height the quadratic gives for these inputs. This is not a measured backtest."""
        return round(float(_predict_one(self, values)), 4)


def fit_response(rows: list | None, inputs: list[str] | tuple[str, ...], score=None, use_log: bool = True) -> QuadraticFit:
    """Fit the quadratic to attempts and solve it.

    `inputs` are parameter names on each attempt's `params` dict, or on the row
    itself. With `use_log`, the fit uses the natural log, which matches stops
    and targets that were doubled rather than stepped by a fixed dollar amount.
    """
    names = tuple(inputs)
    if len(names) < 2:
        raise ValueError("A quadratic peak needs at least two inputs.")
    measure = result_curve if score is None else score
    points = []
    heights = []
    raw_inputs = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        found = _input_values(row, names)
        if found is None:
            continue
        if use_log and any(value <= 0 for value in found):
            continue
        height = measure(row)
        if height is None:
            continue
        raw_inputs.append(found)
        points.append(tuple(math.log(value) if use_log else value for value in found))
        heights.append(float(height))
    if len(points) <= len(_term_names(names)):
        raise ValueError("There are not enough attempts to fit the quadratic.")
    observed = np.asarray(points, dtype=float)
    target = np.asarray(heights, dtype=float)
    center = observed.mean(axis=0)
    spread = observed.std(axis=0)
    spread = np.where(spread < 1e-9, 1.0, spread)
    coded = (observed - center) / spread
    matrix = _design(coded)
    coefficients, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    fitted = matrix @ coefficients
    residual = float(((target - fitted) ** 2).sum())
    total = float(((target - target.mean()) ** 2).sum())
    r2 = 1.0 if total <= 1e-15 else 1.0 - residual / total
    linear, squares, crosses = _split(coefficients, len(names))
    hessian = np.zeros((len(names), len(names)))
    for index, square in enumerate(squares):
        hessian[index, index] = 2.0 * square
    for (left, right), value in crosses:
        hessian[left, right] = value
        hessian[right, left] = value
    critical, eigenvalues, kind = _solve(hessian, linear)
    inputs_at = tuple(float(value) for value in critical * spread + center)
    if use_log:
        inputs_at = tuple(math.exp(value) for value in inputs_at)
    tried = np.asarray(raw_inputs, dtype=float)
    low = tried.min(axis=0)
    high = tried.max(axis=0)
    inside = all(low[index] - 1e-9 <= inputs_at[index] <= high[index] + 1e-9 for index in range(len(names)))
    predicted = _height(coefficients, critical, len(names))
    return QuadraticFit(
        inputs=names,
        count=len(points),
        use_log=use_log,
        mean=tuple(float(value) for value in center),
        scale=tuple(float(value) for value in spread),
        coefficients=tuple(float(value) for value in coefficients),
        system=_system_rows(hessian, linear),
        critical_z=tuple(float(value) for value in critical),
        critical_inputs={name: round(float(inputs_at[index]), 4) for index, name in enumerate(names)},
        predicted=round(predicted, 4),
        eigenvalues=tuple(round(float(value), 6) for value in eigenvalues),
        kind=kind,
        inside_tried_range=inside,
        tried_min={name: round(float(low[index]), 4) for index, name in enumerate(names)},
        tried_max={name: round(float(high[index]), 4) for index, name in enumerate(names)},
        r2=round(r2, 4),
    )


def research_use(fit: QuadraticFit, best_measured: float) -> str:
    """Whether the solved point should be sent to a backtest."""
    if fit.kind != "maximum" or not fit.inside_tried_range:
        return "Do not backtest this point. The quadratic has no peak inside the attempts."
    if fit.predicted <= best_measured + 1e-9:
        return "The solved point does not beat the best attempt already measured."
    return "The solved point is a peak inside the tried range and scores above the best attempt. It is worth one backtest."


def _input_values(row: dict, names: tuple[str, ...]) -> tuple[float, ...] | None:
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    found = []
    for name in names:
        raw = params.get(name, row.get(name))
        try:
            found.append(float(raw))
        except (TypeError, ValueError):
            return None
    return tuple(found)


def _term_names(names: tuple[str, ...]) -> list[str]:
    labels = ["intercept"]
    labels.extend(names)
    labels.extend(f"{name}^2" for name in names)
    labels.extend(f"{names[left]}*{names[right]}" for left in range(len(names)) for right in range(left + 1, len(names)))
    return labels


def _design(coded: np.ndarray) -> np.ndarray:
    count, width = coded.shape
    columns = [np.ones(count)]
    for index in range(width):
        columns.append(coded[:, index])
    for index in range(width):
        columns.append(coded[:, index] ** 2)
    for left in range(width):
        for right in range(left + 1, width):
            columns.append(coded[:, left] * coded[:, right])
    return np.column_stack(columns)


def _split(coefficients: np.ndarray, width: int):
    linear = coefficients[1:1 + width]
    squares = coefficients[1 + width:1 + 2 * width]
    crosses = []
    cursor = 1 + 2 * width
    for left in range(width):
        for right in range(left + 1, width):
            crosses.append(((left, right), float(coefficients[cursor])))
            cursor += 1
    return linear, squares, crosses


def _solve(hessian: np.ndarray, linear: np.ndarray):
    eigenvalues = np.linalg.eigvalsh(hessian)
    try:
        critical = np.linalg.solve(hessian, -linear)
    except np.linalg.LinAlgError:
        critical = np.linalg.lstsq(hessian, -linear, rcond=None)[0]
    negative = bool(np.all(eigenvalues < -1e-8))
    positive = bool(np.all(eigenvalues > 1e-8))
    if negative:
        kind = "maximum"
    elif positive:
        kind = "minimum"
    else:
        kind = "saddle"
    return critical, eigenvalues, kind


def _system_rows(hessian: np.ndarray, linear: np.ndarray) -> tuple[tuple[float, ...], ...]:
    """Each row is one derivative set to zero: coefficients, then the constant moved to the right."""
    rows = []
    for index in range(len(linear)):
        rows.append(tuple(float(value) for value in hessian[index]) + (float(-linear[index]),))
    return tuple(rows)


def _height(coefficients: np.ndarray, coded: np.ndarray, width: int) -> float:
    matrix = _design(np.asarray(coded, dtype=float).reshape(1, width))
    return float((matrix @ coefficients).reshape(-1)[0])


def _predict_one(fit: QuadraticFit, values: dict) -> float:
    coded = []
    for index, name in enumerate(fit.inputs):
        raw = values.get(name)
        if raw is None:
            raise ValueError(f"{name} is required.")
        number = float(raw)
        if fit.use_log:
            if number <= 0:
                raise ValueError(f"{name} has to be positive.")
            number = math.log(number)
        scale = fit.scale[index] or 1.0
        coded.append((number - fit.mean[index]) / scale)
    return _height(np.asarray(fit.coefficients), np.asarray(coded), len(fit.inputs))
