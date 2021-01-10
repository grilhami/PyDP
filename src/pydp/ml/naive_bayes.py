import warnings

import numpy as np # type: ignore
import sklearn.naive_bayes as sk_nb # type: ignore
from sklearn.utils import check_X_y # type: ignore
from sklearn.utils.multiclass import _check_partial_fit_first_call # type: ignore

from .util.accountant import BudgetAccountant
from .util.utils import PrivacyLeakWarning, warn_unused_args
from .util.validation import check_bounds, clip_to_bounds

from .mechanisms.laplace import LaplaceBoundedDomain, LaplaceTruncated
from .mechanisms.geometric import GeometricTruncated


class GaussianNB(sk_nb.GaussianNB):
    r"""Gaussian Naive Bayes (GaussianNB) with differential privacy
    Inherits the :class:`sklearn.naive_bayes.GaussianNB` class from Scikit Learn and adds noise to satisfy differential
    privacy to the learned means and variances.  Adapted from the work presented in [VSB13]_.
    Parameters
    ----------
    epsilon : float, default: 1.0
        Privacy parameter :math:`\epsilon` for the model.
    bounds:  tuple, optional
        Bounds of the data, provided as a tuple of the form (min, max).  `min` and `max` can either be scalars, covering
        the min/max of the entire data, or vectors with one entry per feature.  If not provided, the bounds are computed
        on the data when ``.fit()`` is first called, resulting in a :class:`.PrivacyLeakWarning`.
    priors : array-like, shape (n_classes,)
        Prior probabilities of the classes.  If specified the priors are not adjusted according to the data.
    var_smoothing : float, default: 1e-9
        Portion of the largest variance of all features that is added to variances for calculation stability.
    accountant : BudgetAccountant, optional
        Accountant to keep track of privacy budget.
    Attributes
    ----------
    class_prior_ : array, shape (n_classes,)
        probability of each class.
    class_count_ : array, shape (n_classes,)
        number of training samples observed in each class.
    theta_ : array, shape (n_classes, n_features)
        mean of each feature per class
    sigma_ : array, shape (n_classes, n_features)
        variance of each feature per class
    epsilon_ : float
        absolute additive value to variances (unrelated to ``epsilon`` parameter for differential privacy)
    References
    ----------
    .. [VSB13] Vaidya, Jaideep, Basit Shafiq, Anirban Basu, and Yuan Hong. "Differentially private naive bayes
        classification." In 2013 IEEE/WIC/ACM International Joint Conferences on Web Intelligence (WI) and Intelligent
        Agent Technologies (IAT), vol. 1, pp. 571-576. IEEE, 2013.
    """

    def __init__(
        self,
        epsilon=1.0,
        probability=1e-2,
        bounds=None,
        priors=None,
        var_smoothing=1e-9,
        accountant=None,
    ):
        super().__init__(priors=priors, var_smoothing=var_smoothing)

        self.epsilon = epsilon
        self.bounds = bounds
        self.probability = probability
        self.accountant = BudgetAccountant.load_default(accountant)

    def _partial_fit(self, X, y, classes=None, _refit=False, sample_weight=None):
        self.accountant.check(self.epsilon, 0)

        if sample_weight is not None:
            warn_unused_args("sample_weight")

        X, y = check_X_y(X, y)

        if self.bounds is None:
            warnings.warn(
                "Bounds have not been specified and will be calculated on the data provided. This will "
                "result in additional privacy leakage. To ensure differential privacy and no additional "
                "privacy leakage, specify bounds for each dimension.",
                PrivacyLeakWarning,
            )
            self.bounds = (np.min(X, axis=0), np.max(X, axis=0))

        self.bounds = check_bounds(self.bounds, shape=X.shape[1])
        X = clip_to_bounds(X, self.bounds)

        self.epsilon_ = self.var_smoothing

        if _refit:
            self.classes_ = None

        if _check_partial_fit_first_call(self, classes):
            n_features = X.shape[1]
            n_classes = len(self.classes_)
            self.theta_ = np.zeros((n_classes, n_features))
            self.sigma_ = np.zeros((n_classes, n_features))

            self.class_count_ = np.zeros(n_classes, dtype=np.float64)

            if self.priors is not None:
                priors = np.asarray(self.priors)

                if len(priors) != n_classes:
                    raise ValueError("Number of priors must match number of classes.")
                if not np.isclose(priors.sum(), 1.0):
                    raise ValueError("The sum of the priors should be 1.")
                if (priors < 0).any():
                    raise ValueError("Priors must be non-negative.")
                self.class_prior_ = priors
            else:
                # Initialize the priors to zeros for each class
                self.class_prior_ = np.zeros(len(self.classes_), dtype=np.float64)
        else:
            if X.shape[1] != self.theta_.shape[1]:
                raise ValueError(
                    "Number of features %d does not match previous data %d."
                    % (X.shape[1], self.theta_.shape[1])
                )
            # Put epsilon back in each time
            self.sigma_[:, :] -= self.epsilon_

        classes = self.classes_

        unique_y = np.unique(y)
        unique_y_in_classes = np.in1d(unique_y, classes)

        if not np.all(unique_y_in_classes):
            raise ValueError(
                "The target label(s) %s in y do not exist in the initial classes %s"
                % (unique_y[~unique_y_in_classes], classes)
            )

        noisy_class_counts = self._noisy_class_counts(y)

        for _i, y_i in enumerate(unique_y):
            i = classes.searchsorted(y_i)
            X_i = X[y == y_i, :]

            n_i = noisy_class_counts[_i]

            new_theta, new_sigma = self._update_mean_variance(
                self.class_count_[i],
                self.theta_[i, :],
                self.sigma_[i, :],
                X_i,
                n_noisy=n_i,
            )

            self.theta_[i, :] = new_theta
            self.sigma_[i, :] = new_sigma
            self.class_count_[i] += n_i

        self.sigma_[:, :] += self.epsilon_

        # Update if only no priors is provided
        if self.priors is None:
            # Empirical prior, with sample_weight taken into account
            self.class_prior_ = self.class_count_ / self.class_count_.sum()

        self.accountant.spend(self.epsilon, 0)

        return self

    def _update_mean_variance(
        self, n_past, mu, var, X, sample_weight=None, n_noisy=None
    ):
        """Compute online update of Gaussian mean and variance.
        Given starting sample count, mean, and variance, a new set of points X return the updated mean and variance.
        (NB - each dimension (column) in X is treated as independent -- you get variance, not covariance).
        Can take scalar mean and variance, or vector mean and variance to simultaneously update a number of
        independent Gaussians.
        See Stanford CS tech report STAN-CS-79-773 by Chan, Golub, and LeVeque:
        http://i.stanford.edu/pub/cstr/reports/cs/tr/79/773/CS-TR-79-773.pdf
        Parameters
        ----------
        n_past : int
            Number of samples represented in old mean and variance.  If sample weights were given, this should contain
            the sum of sample weights represented in old mean and variance.
        mu : array-like, shape (number of Gaussians,)
            Means for Gaussians in original set.
        var : array-like, shape (number of Gaussians,)
            Variances for Gaussians in original set.
        sample_weight : ignored
            Ignored in diffprivlib.
        n_noisy : int, optional
            Noisy count of the given class, satisfying differential privacy.
        Returns
        -------
        total_mu : array-like, shape (number of Gaussians,)
            Updated mean for each Gaussian over the combined set.
        total_var : array-like, shape (number of Gaussians,)
            Updated variance for each Gaussian over the combined set.
        """
        if n_noisy is None:
            warnings.warn(
                "Noisy class count has not been specified and will be read from the data. To use this "
                "method correctly, make sure it is run by the parent GaussianNB class.",
                PrivacyLeakWarning,
            )
            n_noisy = X.shape[0]

        if not n_noisy:
            return mu, var

        if sample_weight is not None:
            warn_unused_args("sample_weight")

        # Split epsilon between each feature, using 1/3 of total budget for each of mean and variance
        n_features = X.shape[1]
        local_epsilon = self.epsilon / 3 / n_features

        new_mu = np.zeros((n_features,))
        new_var = np.zeros((n_features,))

        for feature in range(n_features):
            _X = X[:, feature]
            lower, upper = self.bounds[0][feature], self.bounds[1][feature]

            local_diameter = upper - lower

            mech_mu = (
                LaplaceTruncated()
                .set_bounds(lower * n_noisy, upper * n_noisy)
                .set_epsilon(local_epsilon)
                .set_sensitivity(local_diameter)
            )
            _mu = mech_mu.randomise(_X.sum()) / n_noisy

            local_sq_sens = max(_mu - lower, upper - _mu) ** 2
            mech_var = (
                LaplaceBoundedDomain()
                .set_epsilon(local_epsilon)
                .set_sensitivity(local_sq_sens)
                .set_bounds(0, local_sq_sens * n_noisy)
            )
            _var = mech_var.randomise(((_X - _mu) ** 2).sum()) / n_noisy

            new_mu[feature] = _mu
            new_var[feature] = _var

        if n_past == 0:
            return new_mu, new_var

        n_total = float(n_past + n_noisy)

        # Combine mean of old and new data, taking into consideration
        # (weighted) number of observations
        total_mu = (n_noisy * new_mu + n_past * mu) / n_total

        # Combine variance of old and new data, taking into consideration
        # (weighted) number of observations. This is achieved by combining
        # the sum-of-squared-differences (ssd)
        old_ssd = n_past * var
        new_ssd = n_noisy * new_var
        total_ssd = (
            old_ssd
            + new_ssd
            + (n_past / float(n_noisy * n_total))
            * (n_noisy * mu - n_noisy * new_mu) ** 2
        )
        total_var = total_ssd / n_total

        return total_mu, total_var

    def _noisy_class_counts(self, y):
        unique_y = np.unique(y)
        n_total = y.shape[0]

        # Use 1/3 of total epsilon budget for getting noisy class counts
        mech = (
            GeometricTruncated()
            .set_epsilon(self.epsilon / 3)
            .set_sensitivity(1)
            .set_bounds(1, n_total)
            .set_probability(self.probability)
        )
        noisy_counts = np.array([mech.randomise((y == y_i).sum()) for y_i in unique_y])

        argsort = np.argsort(noisy_counts)
        i = 0 if noisy_counts.sum() > n_total else len(unique_y) - 1

        while np.sum(noisy_counts) != n_total:
            _i = argsort[i]
            sgn = np.sign(n_total - noisy_counts.sum())
            noisy_counts[_i] = np.clip(noisy_counts[_i] + sgn, 1, n_total)

            i = (i - sgn) % len(unique_y)

        return noisy_counts
