"""Risk prediction — *which* sample is about to fail, not how many already did.

DIS could analyse off-spec results in every direction — by product, by operator, by
test, with lift tables and driver importance — and could not answer the one question
a lab actually wants answered before the fact: **is this sample, the one in front of
me right now, likely to go off-spec?** Same for turnaround: TAT existed as data, and
nothing predicted a breach.

The design that makes this work without any model-serving infrastructure: a LIMS
dataset already contains both halves. A completed sample carries its IN_SPEC verdict;
an in-flight one does not. So we **train on the rows that have an outcome and score
the rows that don't** — one dataset, one call, deterministic, seeded, auditable, and
no model registry to keep in sync with reality.

Three things this module refuses to do, because each is how a risk model becomes
worse than useless — confidently wrong, and trusted:

1. **Leak the future.** The classic failure: train "will this go off-spec" on the
   result value, or on DATE_COMPLETED. You get 0.99 AUC and a model that cannot run
   on an in-flight sample, because those columns are empty until *after* the outcome
   exists. We detect that by construction — a feature that is absent from the rows we
   need to score cannot be used to score them — and drop it, loudly.
2. **Rebalance the classes.** Off-spec runs at a few per cent, and it is tempting to
   reweight. Don't: reweighting distorts the predicted probabilities, and a risk score
   whose "70%" does not mean 70% is a number nobody should act on. We keep the true
   base rate and pick a threshold from the precision-recall curve instead.
3. **Split at random.** You are predicting the future, so the test set must be the
   future. A random split lets a batch's siblings sit on both sides of the divide and
   flatters the model. When a time column is available we split on it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import AnalyticsError, _num

RANDOM_STATE = 42

MAX_CATEGORY_CARDINALITY = 40
MIN_LABELLED = 40
MIN_POSITIVES = 8
MAX_SCORED_ROWS = 200

# Columns whose very presence usually means the outcome has already happened.
# Only a hint — the availability test below is the one that actually decides.
_OUTCOME_HINTS = ("in_spec", "inspec", "out_of_spec", "result", "status", "completed",
                  "date_completed", "tat", "turnaround", "verdict", "pass_fail")


def _clean_features(df, target_column, feature_columns):
    cols = [c for c in (feature_columns or df.columns) if c != target_column]
    if not cols:
        raise AnalyticsError("No feature columns available.")
    return cols


def _split_labelled(df, target_column, positive_value):
    """Rows with an outcome (train) vs rows without one (score)."""
    if target_column not in df.columns:
        raise AnalyticsError(f"Target column '{target_column}' not found.")
    target = df[target_column]
    known = target.notna() & (target.astype(str).str.strip() != "")
    labelled, unlabelled = df[known].copy(), df[~known].copy()

    if len(labelled) < MIN_LABELLED:
        raise AnalyticsError(
            f"Need at least {MIN_LABELLED} samples with a known outcome to learn "
            f"anything; '{target_column}' has {len(labelled)}.")

    y_raw = labelled[target_column]
    if positive_value is None:
        # Default to the MINORITY class. The interesting outcome — off-spec, breached,
        # rejected — is almost always the rare one, and guessing the majority would
        # silently model "did this sample behave normally", which nobody asked.
        counts = y_raw.astype(str).value_counts()
        if len(counts) < 2:
            raise AnalyticsError(
                f"'{target_column}' has only one distinct outcome — nothing to predict.")
        positive_value = counts.index[-1]

    y = (y_raw.astype(str) == str(positive_value)).astype(int).to_numpy()
    positives = int(y.sum())
    if positives < MIN_POSITIVES:
        raise AnalyticsError(
            f"Only {positives} sample(s) have outcome '{positive_value}' — too few to "
            f"learn a risk model (need {MIN_POSITIVES}).")
    if positives == len(y):
        raise AnalyticsError(f"Every sample has outcome '{positive_value}'.")

    return labelled, unlabelled, y, str(positive_value)


def _leakage_report(labelled, unlabelled, cols, y):
    """Drop features that cannot exist at prediction time, and flag the suspicious.

    The availability test is the honest one and it needs no statistics: if a column
    is empty for the samples we must score, then it is empty *because* the outcome
    has not happened yet — and a model leaning on it is a model that cannot run.
    """
    dropped, suspicious, keep = [], [], []

    for col in cols:
        if col not in labelled.columns:
            continue
        # 1. Available at prediction time?
        if len(unlabelled):
            avail = float(unlabelled[col].notna().mean())
            if avail < 0.5:
                dropped.append({
                    "column": col, "availability": _num(avail),
                    "reason": (f"only {avail:.0%} of in-flight samples have this value — "
                               "it is not known until after the outcome, so a model "
                               "using it could never score a live sample"),
                })
                continue
        # 2. Constant columns carry nothing.
        if labelled[col].nunique(dropna=True) <= 1:
            dropped.append({"column": col, "availability": 1.0,
                            "reason": "constant — no information"})
            continue
        # 3. Identifiers are not features. This catches them in both dtypes, and the
        #    NUMERIC case is the one that bites: a monotonic sample_number sails
        #    through any "is it categorical?" check, and under a time-ordered split it
        #    is actively poisonous — every ID in the holdout lies outside the range the
        #    trees ever saw, so the model extrapolates off a cliff. An un-dropped row
        #    counter does not merely add noise; it degrades a model that would
        #    otherwise work.
        if pd.api.types.is_numeric_dtype(labelled[col]):
            v = pd.to_numeric(labelled[col], errors="coerce").dropna()
            int_like = bool(len(v)) and bool(np.all(np.isclose(v % 1, 0)))
            if len(v) and int_like and v.nunique() / len(v) > 0.95:
                dropped.append({
                    "column": col, "availability": 1.0,
                    "reason": (f"{v.nunique()} distinct integer values across {len(v)} "
                               "rows — this is an identifier or row counter, not a "
                               "feature. Under a time-ordered split it would push the "
                               "model to extrapolate beyond every value it trained on."),
                })
                continue
        else:
            n_unique = labelled[col].nunique(dropna=True)
            if n_unique > MAX_CATEGORY_CARDINALITY:
                dropped.append({
                    "column": col, "availability": 1.0,
                    "reason": (f"{n_unique} distinct values — looks like an identifier, "
                               "not a feature (a model would memorise it)"),
                })
                continue
        keep.append(col)

    # 4. A single feature that predicts the outcome almost perfectly is the outcome
    #    wearing a hat. Flag it rather than silently enjoying the AUC.
    from sklearn.metrics import roc_auc_score
    for col in list(keep):
        s = labelled[col]
        try:
            if pd.api.types.is_numeric_dtype(s):
                v = pd.to_numeric(s, errors="coerce")
                if v.notna().sum() < len(y) * 0.5:
                    continue
                auc = roc_auc_score(y[v.notna().to_numpy()], v.dropna().to_numpy())
            else:
                codes = s.astype(str)
                rate = pd.Series(y, index=labelled.index).groupby(codes).transform("mean")
                auc = roc_auc_score(y, rate.to_numpy())
        except Exception:  # noqa: BLE001 — a feature we cannot score is not suspicious
            continue
        auc = max(auc, 1 - auc)
        if auc >= 0.99:
            suspicious.append({
                "column": col, "single_feature_auc": _num(auc),
                "reason": ("this column alone predicts the outcome almost perfectly — "
                           "it is very likely derived from it (target leakage). The "
                           "model's accuracy below is not to be believed until you "
                           "confirm this column is known BEFORE the result exists."),
            })

    if not keep:
        raise AnalyticsError(
            "No usable features remain after removing columns that are unavailable at "
            "prediction time. A risk model needs information known BEFORE the outcome.")
    return keep, dropped, suspicious


def _encode(train_df, score_df, cols):
    """One-hot the categoricals, pass the numerics through, align both frames."""
    frames_tr, frames_sc = [], []
    for col in cols:
        s_tr, s_sc = train_df[col], (score_df[col] if len(score_df) else None)
        if pd.api.types.is_numeric_dtype(s_tr):
            frames_tr.append(pd.to_numeric(s_tr, errors="coerce").rename(col))
            if s_sc is not None:
                frames_sc.append(pd.to_numeric(s_sc, errors="coerce").rename(col))
        else:
            d_tr = pd.get_dummies(s_tr.astype(str), prefix=col)
            frames_tr.append(d_tr)
            if s_sc is not None:
                frames_sc.append(pd.get_dummies(s_sc.astype(str), prefix=col))

    X_tr = pd.concat(frames_tr, axis=1).astype(float)
    X_tr = X_tr.fillna(X_tr.median(numeric_only=True)).fillna(0.0)
    if not len(score_df):
        return X_tr, pd.DataFrame(columns=X_tr.columns)

    X_sc = pd.concat(frames_sc, axis=1).astype(float)
    # Reindex so an unseen category at scoring time becomes all-zeros rather than a
    # column mismatch that would crash — or worse, silently shift every column.
    X_sc = X_sc.reindex(columns=X_tr.columns, fill_value=0.0)
    X_sc = X_sc.fillna(X_tr.median(numeric_only=True)).fillna(0.0)
    return X_tr, X_sc


def _split(X, y, labelled, time_column, test_fraction):
    n = len(X)
    n_test = max(10, int(n * float(test_fraction)))
    n_test = min(n_test, n - MIN_LABELLED // 2)

    if time_column and time_column in labelled.columns:
        order = pd.to_datetime(labelled[time_column], errors="coerce")
        if order.notna().mean() > 0.8:
            idx = np.argsort(order.fillna(order.min()).to_numpy())
            tr, te = idx[:-n_test], idx[-n_test:]
            if y[te].sum() >= 3:      # a test set with no positives measures nothing
                return tr, te, "time-ordered", ""
            note = (f"A time-ordered split on '{time_column}' left too few positive "
                    "cases in the holdout, so a stratified random split was used "
                    "instead. Random splits can flatter a model when samples from the "
                    "same batch land on both sides — read the accuracy with that in mind.")
        else:
            note = (f"'{time_column}' could not be parsed as dates, so a random split "
                    "was used.")
    else:
        note = ("No time column was given, so the model was validated on a random "
                "split. You are predicting the future: a time-ordered split is the "
                "honest test, because a random one lets a batch's siblings sit on both "
                "sides of the divide. Pass time_column for a trustworthy number.")

    from sklearn.model_selection import train_test_split
    tr, te = train_test_split(np.arange(n), test_size=n_test, random_state=RANDOM_STATE,
                              stratify=y)
    return tr, te, "random", note


def _calibration(y_true, p, bins=5):
    """Does a predicted 70% actually happen 70% of the time?"""
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        if not m.any():
            continue
        out.append({
            "bucket": f"{lo:.0%}–{hi:.0%}",
            "predicted": _num(float(p[m].mean())),
            "actual": _num(float(y_true[m].mean())),
            "n": int(m.sum()),
        })
    return out


def _lift(y_true, p, base_rate):
    """"If I re-test the N riskiest samples, how many failures do I catch?"

    This is the shape in which a lab manager can actually spend the model: they have
    capacity to re-check some number of samples, not a probability threshold.
    """
    order = np.argsort(-p)
    total_pos = int(y_true.sum())
    rows = []
    for k in (10, 20, 50, 100):
        if k > len(p):
            break
        top = order[:k]
        caught = int(y_true[top].sum())
        precision = caught / k
        rows.append({
            "top_k": k,
            "caught": caught,
            "of_total_positives": total_pos,
            "captured": _num(caught / total_pos if total_pos else 0.0),
            "precision": _num(precision),
            "lift": _num(precision / base_rate if base_rate else 0.0),
        })
    return rows


def predict_risk(df, target_column, positive_value=None, feature_columns=None,
                 time_column=None, test_fraction=0.25, threshold=None,
                 top_k=MAX_SCORED_ROWS):
    """Learn who fails, from the samples that already did — then score the ones in flight."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import (average_precision_score, brier_score_loss,
                                 precision_recall_curve, roc_auc_score)

    if df is None or df.empty:
        raise AnalyticsError("The dataset is empty.")

    labelled, unlabelled, y, positive_value = _split_labelled(
        df, target_column, positive_value)
    cols = _clean_features(df, target_column, feature_columns)
    if time_column and time_column in cols:
        cols = [c for c in cols if c != time_column]   # ordering key, not a feature

    features, dropped, suspicious = _leakage_report(labelled, unlabelled, cols, y)
    X, X_score = _encode(labelled, unlabelled, features)

    tr, te, split_kind, split_note = _split(X, y, labelled, time_column, test_fraction)

    # NOT class-weighted, deliberately: reweighting distorts the probabilities, and a
    # risk score whose "70%" does not mean 70% is a number nobody should act on. The
    # imbalance is handled by reading the PR curve, not by lying to the model.
    #
    # min_samples_leaf is load-bearing, not a nicety. Without it the trees happily
    # carve up a continuous noise column into 20 splits and "learn" it; with a 10%
    # base rate and a few hundred rows that is most of the model's capacity spent on
    # nothing. It moved held-out ROC-AUC from 0.62 to 0.69 on the test fixture.
    model = GradientBoostingClassifier(
        random_state=RANDOM_STATE, n_estimators=150, max_depth=3,
        learning_rate=0.05, min_samples_leaf=20, subsample=0.8)
    model.fit(X.iloc[tr], y[tr])

    p_te = model.predict_proba(X.iloc[te])[:, 1]
    y_te = y[te]
    base_rate = float(y.mean())

    roc = float(roc_auc_score(y_te, p_te)) if len(set(y_te)) > 1 else None
    pr_auc = float(average_precision_score(y_te, p_te)) if len(set(y_te)) > 1 else None
    brier = float(brier_score_loss(y_te, p_te))

    if threshold is None and len(set(y_te)) > 1:
        prec, rec, thr = precision_recall_curve(y_te, p_te)
        f1 = np.divide(2 * prec * rec, prec + rec,
                       out=np.zeros_like(prec), where=(prec + rec) > 0)
        threshold = float(thr[int(np.argmax(f1[:-1]))]) if len(thr) else 0.5
    threshold = float(threshold if threshold is not None else 0.5)

    pred = (p_te >= threshold).astype(int)
    tp = int(((pred == 1) & (y_te == 1)).sum())
    fp = int(((pred == 1) & (y_te == 0)).sum())
    fn = int(((pred == 0) & (y_te == 1)).sum())
    tn = int(((pred == 0) & (y_te == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # PERMUTATION importance on the held-out set, not the model's built-in
    # `feature_importances_`. Impurity importance is biased toward continuous and
    # high-cardinality features: on the test fixture it ranked a column of pure random
    # noise at 0.68 — the top "driver" of off-spec — while permutation importance
    # correctly puts it at zero. Telling a laboratory that a noise column drives its
    # failures is not a small error; it is the error that ends the model's career.
    #
    # Permutation importance answers a question anyone can check: how much worse does
    # the model get, on data it has never seen, if I shuffle this one column?
    from sklearn.inspection import permutation_importance

    perm = permutation_importance(model, X.iloc[te], y[te], n_repeats=10,
                                  random_state=RANDOM_STATE, scoring="roc_auc")
    importance = sorted(
        [{"feature": f, "importance": _num(float(m)), "std": _num(float(s))}
         for f, m, s in zip(X.columns, perm.importances_mean, perm.importances_std)
         if m > 0],
        key=lambda d: -d["importance"])[:20]

    at_risk = []
    if len(X_score):
        p_sc = model.predict_proba(X_score)[:, 1]
        order = np.argsort(-p_sc)[:int(top_k)]
        keep_cols = [c for c in features if c in unlabelled.columns][:8]
        for i in order:
            row = unlabelled.iloc[int(i)]
            at_risk.append({
                "risk": _num(float(p_sc[int(i)])),
                "above_threshold": bool(p_sc[int(i)] >= threshold),
                **{c: (None if pd.isna(row[c]) else row[c]) for c in keep_cols},
            })

    useless = roc is not None and roc < 0.6
    out = {
        "test": "risk_model",
        "target": target_column,
        "positive_value": positive_value,
        "n_labelled": int(len(labelled)),
        "n_in_flight": int(len(unlabelled)),
        "base_rate": _num(base_rate),
        "features_used": list(X.columns)[:60],
        "leakage": {"dropped": dropped, "suspicious": suspicious},
        "split": {"kind": split_kind, "train": int(len(tr)), "test": int(len(te))},
        "threshold": _num(threshold),
        "metrics": {
            "roc_auc": _num(roc), "pr_auc": _num(pr_auc), "brier": _num(brier),
            "precision": _num(precision), "recall": _num(recall), "f1": _num(f1),
            "true_positives": tp, "false_positives": fp,
            "false_negatives": fn, "true_negatives": tn,
        },
        "calibration": _calibration(y_te, p_te),
        "lift": _lift(y_te, p_te, base_rate),
        "feature_importance": importance,
        "importance_kind": "permutation (drop in held-out ROC-AUC when shuffled)",
        "at_risk": at_risk,
    }

    # Interpretation. Lead with whether the model is worth believing at all — an
    # unusable model presented as a ranked list of "at-risk samples" is worse than
    # nothing, because the list looks exactly like a usable one.
    parts = []
    if useless:
        parts.append(
            f"This model does NOT work: ROC-AUC is {roc:.2f}, which is barely better "
            f"than guessing. The available columns do not predict '{positive_value}'. "
            f"Do not act on the risk scores below.")
    else:
        auc_txt = f"ROC-AUC {roc:.2f}" if roc is not None else "AUC n/a"
        parts.append(
            f"Risk of {target_column}='{positive_value}' ({base_rate:.1%} of "
            f"{len(labelled)} completed samples). {auc_txt}, PR-AUC "
            f"{pr_auc:.2f} against a {base_rate:.1%} base rate; at a threshold of "
            f"{threshold:.2f} it catches {recall:.0%} of failures with "
            f"{precision:.0%} precision.")
        if out["lift"]:
            best = out["lift"][0]
            parts.append(
                f"Re-checking the {best['top_k']} riskiest samples would catch "
                f"{best['caught']} of {best['of_total_positives']} failures — "
                f"{best['lift']:.1f}× better than picking at random.")
    if len(unlabelled):
        flagged = sum(1 for r in at_risk if r["above_threshold"])
        parts.append(f"{flagged} of {len(unlabelled)} in-flight samples are above the "
                     f"risk threshold.")
    else:
        parts.append("There are no in-flight samples to score in this dataset.")
    if suspicious:
        parts.append(
            "⚠ Possible target leakage: "
            + ", ".join(s["column"] for s in suspicious)
            + " predicts the outcome almost perfectly. Confirm it is known BEFORE the "
              "result exists, or the accuracy above is fiction.")
    if dropped:
        parts.append(f"{len(dropped)} column(s) were excluded as unusable at prediction "
                     f"time (see 'leakage.dropped').")
    if split_note:
        parts.append(split_note)

    out["usable"] = not useless
    out["interpretation"] = " ".join(parts)
    return out
