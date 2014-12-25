from __future__ import division
import numpy as np
from scipy.optimize import fmin_bfgs
from itertools import combinations_with_replacement, chain

class Basic(object):


	def __init__(self, Y, D, X):

		self.Y, self.D, self.X = Y, D, X
		self.N, self.K = self.X.shape
		self.Y_t, self.Y_c = self.Y[self.D==1], self.Y[self.D==0]
		self.X_t, self.X_c = self.X[self.D==1], self.X[self.D==0]
		self.N_t = self.D.sum()
		self.N_c = self.N - self.N_t


	@property
	def ndiff(self):

		try:
			return self._ndiff
		except AttributeError:
			self._ndiff = self._compute_ndiff()
			return self._ndiff


	def _compute_ndiff(self):

		"""
		Computes normalized difference in covariates for assessing balance.

		Normalized difference is the difference in group means, scaled by the
		square root of the average of the two within-group variances. Large
		values indicate that simple linear adjustment methods may not be adequate
		for removing biases that are associated with differences in covariates.

		Unlike t-statistic, normalized differences do not, in expectation,
		increase with sample size, and thus is more appropriate for assessing
		balance.

		Returns
		-------
			Vector of normalized differences.
		"""

		return (self.X_t.mean(0) - self.X_c.mean(0)) / \
		       np.sqrt((self.X_t.var(0) + self.X_c.var(0))/2)


	def _try_del(self, attrstr):
	
		try:
			delattr(self, attrstr)
		except:
			pass


class Stratum(Basic):


	def __init__(self, Y, D, X, pscore):

		super(Stratum, self).__init__(Y, D, X)
		self.pscore = {'fitted': pscore, 'min': pscore.min(),
		               'mean': pscore.mean(), 'max': pscore.max()}


	@property
	def within(self):
	
		try:
			return self._within
		except AttributeError:
			self._within = self._compute_within()
			return self._within


	def _compute_within(self):
	
		Z = np.empty((self.N, self.K+2))
		Z[:,0], Z[:,1], Z[:,2:] = 1, self.D, self.X
		self._olscoeff = np.linalg.lstsq(Z, self.Y)[0]

		return self._olscoeff[1]


	@property
	def olscoeff(self):
	
		try:
			return self._olscoeff
		except AttributeError:
			self._within = self._compute_within()
			return self._olscoeff


	@property
	def stderr(self):

		try:
			return self._stderr
		except AttributeError:
			self._stderr = self._compute_stderr()
			return self._stderr


	def _compute_stderr(self):

		u = self.Y - (self.olscoeff[0] + self.D*self.olscoeff[1] + self.X.dot(self.olscoeff[2:]))
		e = np.zeros(self.K+2); e[1] = 1
		Z = np.empty((self.N, self.K+2))
		Z[:,0], Z[:,1], Z[:,2:] = 1, self.D, self.X
		w = np.linalg.solve(Z.T.dot(Z), e)

		return np.sqrt(np.einsum('j,k,ij,ik,i,i',w,w,Z,Z,u,u))


class CausalModel(Basic):


	def __init__(self, Y, D, X):

		super(CausalModel, self).__init__(Y, D, X)
		self._Y_old, self._D_old, self._X_old = Y, D, X


	@property
	def Xvar(self):

		try:
			return self._Xvar
		except AttributeError:
			self._Xvar = self.X.var(0)
			return self._Xvar


	def restart(self):

		"""
		Reinitialize data to original inputs, and drop any estimated results.
		"""

		super(CausalModel, self).__init__(self._Y_old, self._D_old, self._X_old)
		self._try_del('pscore')
		self._try_del('cutoff')
		self._try_del('blocks')
		self._try_del('ATE')
		self._try_del('ATT')
		self._try_del('ATC')


	def _sigmoid(self, x):
	
		"""
		Computes 1/(1+exp(-x)) for input x, to be used in maximum likelihood
		estimation of propensity score.

		Expected args
		-------------
			x: array-like

		Returns
		-------
			Vector or scalar 1/(1+exp(-x)), depending on input x.
		"""

		return 1/(1+np.exp(-x))


	def _log1exp(self, x):

		"""
		Computes log(1+exp(-x)) for input x, to be used in maximum likelihood
		estimation of propensity score.

		Expected args
		-------------
			x: array-like

		Returns
		-------
			Vector or scalar log(1+exp(-x)), depending on input x.
		"""

		return np.log(1 + np.exp(-x))


	def _neg_loglike(self, beta, X_t, X_c):

		"""
		Computes the negative of the log likelihood function for logit, to be used
		in maximum likelihood estimation of propensity score. Negative because SciPy
		optimizier does minimization only.

		Expected args
		-------------
			beta: array-like
				Logisitic regression parameters to maximize over.
			X_t: matrix, ndarray
				Covariate matrix of the treated units.
			X_c: matrix, ndarray
				Covariate matrix of the control units.

		Returns
		-------
			Negative log likelihood evaluated at input values.
		"""

		return self._log1exp(X_t.dot(beta)).sum() + \
		       self._log1exp(-X_c.dot(beta)).sum()


	def _neg_gradient(self, beta, X_t, X_c):

		"""
		Computes the negative of the gradient of the log likelihood function for
		logit, to be used in maximum likelihood estimation of propensity score.
		Negative because SciPy optimizier does minimization only.

		Expected args
		-------------
			beta: array-like
				Logisitic regression parameters to maximize over.
			X_t: matrix, ndarray
				Covariate matrix of the treated units.
			X_c: matrix, ndarray
				Covariate matrix of the control units.

		Returns
		-------
			Negative gradient of log likelihood function evaluated at input values.
		"""

		return (self._sigmoid(X_c.dot(beta))*X_c.T).sum(1) - \
		       (self._sigmoid(-X_t.dot(beta))*X_t.T).sum(1)


	def _compute_pscore(self, X):

		"""
		Estimates via logit the propensity score based on input covariate matrix X.

		Expected args
		-------------
			X: matrix, ndarray
				Covariate matrix to estimate propensity score on.

		Returns
		-------
			pscore: dict containing
				'coeff': Estimated coefficients.
				'loglike': Maximized log-likelihood value.
				'fitted': Vector of estimated propensity scores.
		"""

		X_t = X[self.D==1]
		X_c = X[self.D==0]
		K = X.shape[1]

		neg_loglike = lambda x: self._neg_loglike(x, X_t, X_c)
		neg_gradient = lambda x: self._neg_gradient(x, X_t, X_c)

		logit = fmin_bfgs(neg_loglike, np.zeros(K), neg_gradient, full_output=True, disp=False)

		pscore = {}
		pscore['coeff'], pscore['loglike'] = logit[0], -logit[1]
		pscore['fitted'] = np.empty(self.N)
		pscore['fitted'][self.D==1] = self._sigmoid(X_t.dot(pscore['coeff']))
		pscore['fitted'][self.D==0] = self._sigmoid(X_c.dot(pscore['coeff']))

		return pscore


	def _form_matrix(self, lin, qua):

		"""
		Forms covariate matrix for use in propensity score estimation, based on
		requirements on constant term, linear terms, and quadratic terms.

		Expected args
		-------------
			const: Boolean
				Includes a column of one's if True.
			lin: list
				Column numbers (one-based) of the original covariate
				matrix to include linearly.
			qua: list
				Tuples indicating which columns of the original
				covariate matrix to multiply and include. E.g.,
				[(1,1), (2,3)] indicates squaring the 1st column and
				including the product of the 2nd and 3rd columns.

		Returns
		-------
			mat: matrix, ndarray
				Covariate matrix formed based on requirements on
				linear and quadratic terms.
		"""

		mat = np.empty((self.N, 1+len(lin)+len(qua)))

		mat[:, 0] = 1
		current_col = 1
		if lin:
			mat[:, current_col:current_col+len(lin)] = self.X[:, lin]
			current_col += len(lin)
		for term in qua:
			mat[:, current_col] = self.X[:, term[0]] * self.X[:, term[1]]
			current_col += 1

		return mat


	def _change_base(self, l, pair=False, base=0):

		"""
		Changes input index to zero or one-based.

		Expected args
		-------------
			l: list
				List of numbers or pairs of numbers.
			pair: Boolean
				Anticipates list of pairs if True. Defaults to False.
			base: integer
				Converts to zero-based if 0, one-based if 1.

		Returns
		-------
			Input index with base changed.
		"""

		offset = 2*base - 1
		if pair:
			return [(p[0]+offset, p[1]+offset) for p in l]
		else:
			return [e+offset for e in l]
			

	def _post_pscore_init(self):

		"""
		Initialize cutoff threshold for trimming and number of equal-sized
		blocks after estimation of propensity score.
		"""

		if not hasattr(self, 'cutoff'):
			self.cutoff = 0.1
		if not hasattr(self, 'blocks'):
			self.blocks = 5


	def propensity(self, lin='all', qua=[]):

		"""
		Estimates via logit the propensity score based on requirements on
		constant term, linear terms, and quadratic terms.

		Expected args
		-------------
			lin: string, list
				Column numbers of the original covariate matrix X
				to include linearly. Defaults to the string 'all',
				which uses whole covariate matrix.
			qua: list
				Column numbers of the original covariate matrix X
				to include quadratic. E.g., [1,3] will include
				squares of the 1st and 3rd columns, and the product
				of these two columns. Default is to not include any
				quadratic terms.
		"""

		if lin == 'all':
			lin = xrange(self.X.shape[1])
		else:
			lin = self._change_base(lin, base=0)
		qua = self._change_base(qua, pair=True, base=0)

		self.pscore = self._compute_pscore(self._form_matrix(lin, qua))
		self.pscore['lin'], self.pscore['qua'] = lin, qua

		self._post_pscore_init()


	def _select_terms(self, cur, pot, crit, lin=[]):
	
		"""
		Estimates via logit the propensity score using Imbens and Rubin's
		covariate selection algorithm.

		Expected args
		-------------
			cur: list
				List containing terms that are currently included
				in the logistic regression.
			pot: list
				List containing candidate terms to be iterated through.
			crit: scalar
				Critical value used in likelihood ratio test to decide
				whether candidate terms should be included.
			lin: list
				List containing linear terms that have been decided on.
				If non-empty, then cur and pot should be containing
				candidate quadratic terms. If empty, then those two
				matrices should be containing candidate linear terms.

		Returns
		-------
			List containing terms that the algorithm has settled on for inclusion.
		"""

		if not pot:
			return cur

		if not lin:  # lin is empty, so linear terms not yet decided
			ll_null = self._compute_pscore(self._form_matrix(cur, []))['loglike']
		else:  # lin is not empty, so linear terms are already fixed
			ll_null = self._compute_pscore(self._form_matrix(lin, cur))['loglike']

		lr = np.empty(len(pot))
		if not lin:
			for i in xrange(len(pot)):
				lr[i] = 2*(self._compute_pscore(self._form_matrix(cur+[pot[i]], []))['loglike'] - ll_null)
		else:
			for i in xrange(len(pot)):
				lr[i] = 2*(self._compute_pscore(self._form_matrix(lin, cur+[pot[i]]))['loglike'] - ll_null)

		argmax = np.argmax(lr)
		if lr[argmax] < crit:
			return cur
		else:
			new_term = pot.pop(argmax)
			return self._select_terms(cur+[new_term], pot, crit, lin)


	def propensity_s(self, lin_B=[], C_lin=1, C_qua=2.71):

		"""
		Estimates via logit the propensity score using Imbens and Rubin's
		covariate selection algorithm.

		Expected args
		-------------
			lin_B: list
				Column numbers of the original covariate matrix X
				that should be included as linear terms
				regardless. Defaults to empty list, meaning
				every column of X is subjected to the selection
				algorithm.
			C_lin: scalar
				Critical value used in likelihood ratio test to decide
				whether candidate linear terms should be included.
				Defaults to 1 as in Imbens (2014).
			C_qua: scalar
				Critical value used in likelihood ratio test to decide
				whether candidate quadratic terms should be included.
				Defaults to 2.71 as in Imbens (2014).

		References
		----------
			Imbens, G. & Rubin, D. (2015). Causal Inference in Statistics,
				Social, and Biomedical Sciences: An Introduction.
			Imbens, G. (2014). Matching Methods in Practice: Three Examples.
		"""

		lin_B = self._change_base(lin_B, base=0)
		if C_lin == 0:
			lin = xrange(self.X.shape[1])
		else:
			pot = list(set(xrange(self.X.shape[1])) - set(lin_B))
			lin = self._select_terms(lin_B, pot, C_lin)

		if C_qua == np.inf:
			qua = []
		elif C_qua == 0:
			qua = list(combinations_with_replacement(lin, 2))
		else:
			pot = list(combinations_with_replacement(lin, 2))
			qua = self._select_terms([], pot, C_qua, lin)

		self.pscore = self._compute_pscore(self._form_matrix(lin, qua))
		self.pscore['lin'], self.pscore['qua'] = lin, qua

		self._post_pscore_init()


	def _check_prereq(self, prereq):

		"""
		Basic checks of existence of estimated propensity score or strata.
		"""

		if not hasattr(self, prereq):
			if prereq == 'pscore':
				raise Exception("Missing propensity score.")
			if prereq == 'strata':
				raise Exception("Please stratify sample.")
	

	def trim(self):

		"""
		Trims data based on propensity score to create a subsample with better
		covariate balance. The CausalModel class has cutoff has a property,
		with default value 0.1. User can modify this directly, or by calling
		select_cutoff to have the cutoff selected automatically using the
		algorithm proposed by Crump, Hotz, Imbens, and Mitnik.
		"""

		self._check_prereq('pscore')
		untrimmed = (self.pscore['fitted'] >= self.cutoff) & (self.pscore['fitted'] <= 1-self.cutoff)
		super(CausalModel, self).__init__(self.Y[untrimmed], self.D[untrimmed], self.X[untrimmed])
		self.pscore['fitted'] = self.pscore['fitted'][untrimmed]
		self._try_del('_ndiff')


	def _select_cutoff(self):

		"""
		Selects cutoff value for propensity score used in trimming function
		using algorithm suggested by Crump, Hotz, Imbens, and Mitnik (2008).
		
		References
		----------
			Crump, R., Hotz, V., Imbens, G., & Mitnik, O. (2008). Dealing
				with Limited Overlap in Estimation of Average Treatment
				Effects, Biometrika.
		"""

		g = 1 / (self.pscore['fitted'] * (1-self.pscore['fitted']))
		order = np.argsort(g)
		h = g[order].cumsum()/np.square(xrange(1,self.N+1))
		
		self.cutoff = 0.5 - np.sqrt(0.25-1/g[order[h.argmin()]])

	
	def trim_s(self):

		"""
		Trim data based on propensity score using cutoff selected using
		algorithm suggested by Crump, Hotz, Imbens, and Mitnik (2008).
		Algorithm is of order O(N).

		References
		----------
			Crump, R., Hotz, V., Imbens, G., & Mitnik, O. (2008). Dealing
				with Limited Overlap in Estimation of Average Treatment
				Effects, Biometrika.
		"""

		self._check_prereq('pscore')
		self._select_cutoff()
		self.trim()


	def stratify(self):

		"""
		Stratify the sample based on propensity score. If the attribute cutoff is
		a number, then equal-sized bins will be created. Otherwise if cutoff is a
		list of bin boundaries then the bins will be created accordingly.
		"""

		self._check_prereq('pscore')
		if isinstance(self.blocks, (int, long)):
			q = list(np.linspace(0,100,self.blocks+1))[1:-1]
			self.blocks = [0] + np.percentile(self.pscore['fitted'], q) + [1]

		self.strata = [None] * (len(self.blocks)-1)
		for i in xrange(len(self.blocks)-1):
			subclass = (self.pscore['fitted']>self.blocks[i]) & \
			           (self.pscore['fitted']<=self.blocks[i+1])
			Y = self.Y[subclass]
			D = self.D[subclass]
			X = self.X[subclass]
			self.strata[i] = Stratum(Y, D, X, self.pscore['fitted'][subclass])


	def _select_blocks(self, e, l, e_min, e_max):

		"""
		Select propensity bins recursively for blocking estimator using
		algorithm suggested by Imbens and Rubin (2015). Algorithm is of
		order O(N log N).

		Expected args
		-------------
			e: array-like
				Vector of estimated propensity scores for the
				whole sample.
			l: array-like
				Vector of log odds ratio for the whole sample.
			e_min: scalar
				Lower boundary of current propensity bin.
			e_max: scalar
				Upper boundary of current propensity bin.

		Returns
		-------
			List containing bin boundaries.

		References
		----------
			Imbens, G. & Rubin, D. (2015). Causal Inference in Statistics,
				Social, and Biomedical Sciences: An Introduction.
		"""

		scope = (e >= e_min) & (e <= e_max)
		t, c = (scope & (self.D==1)), (scope & (self.D==0))

		N_t, N_c = t.sum(), c.sum()
		t_stat = (l[t].mean()-l[c].mean()) / \
		         np.sqrt(l[t].var()/t.sum() + l[c].var()/c.sum())
		if t_stat <= 1.96:
			return [e_min, e_max]

		med = e[e <= np.median(e[scope])].max()
		left = (e <= med) & scope
		right = (e > med) & scope
		N_left = left.sum()
		N_right = right.sum()
		N_left_t = (left & (self.D==1)).sum()
		N_right_t = (right & (self.D==1)).sum()

		if np.min([N_left, N_right]) <= self.K+2:
			return [e_min, e_max]
		if np.min([N_left_t, N_left-N_left_t, N_right_t, N_right-N_right_t]) <= 3:
			return [e_min, e_max]

		return self._select_blocks(e, l, e[left].min(), med) + \
		       self._select_blocks(e, l, med, e[right].max())


	def stratify_s(self):

		"""
		Stratify the sample based on propensity score using bin selection
		algorithm suggested by Imbens and Rubin (2015).

		References
		----------
			Imbens, G. & Rubin, D. (2015). Causal Inference in Statistics,
				Social, and Biomedical Sciences: An Introduction.
		"""

		self._check_prereq('pscore')
		l = np.log(self.pscore['fitted'] / (1+self.pscore['fitted']))
		e_min = self.pscore['fitted'].min()
		e_max = self.pscore['fitted'].max()
		self.blocks = sorted(set(self._select_blocks(self.pscore['fitted'], l, e_min, e_max)))
		self.stratify()


	def blocking(self):

		"""
		Compute average treatment effects using regression within blocks.
		"""

		self._check_prereq('strata')
		self.ATE = np.sum([stratum.N/self.N * stratum.within for stratum in self.strata])
		self.ATT = np.sum([stratum.N_t/self.N_t * stratum.within for stratum in self.strata])
		self.ATC = np.sum([stratum.N_c/self.N_c * stratum.within for stratum in self.strata])


	def _norm(self, dX, W):

		"""
		Calculates vector of norms given weighting matrix W.

		Expected args
		-------------
			dX: array-like
				Matrix of covariate differences.
			W: string or matrix, ndarray
				Weighting matrix to be used in norm calcuation. Acceptable
				values are string 'inv' for inverse variance weighting,
				or any arbitrary K-by-K matrix.

		Returns
		-------
			Vector of distance measures.
		"""

		if W == 'inv':
			return (dX**2 / self.Xvar).sum(axis=1)
		else:
			return (dX.dot(W)*dX).sum(axis=1)


	def _msmallest_with_ties(self, x, m):

		"""
		Finds indices of the m smallest entries in an array. Ties are
		included, so the number of indices can be greater than m. Algorithm
		is of order O(n).

		Expected args
		-------------
			x: array-like
				Array of numbers to find m smallest entries for.
			m: integer
				Number of smallest entries to find.

		Returns
		-------
			List of indices of smallest entries.
		"""

		par_indx = np.argpartition(x, m)  # partition around (m+1)th order stat
		
		if x[par_indx[:m]].max() < x[par_indx[m]]:  # mth < (m+1)th order stat
			return list(par_indx[:m])
		elif x[par_indx[m]] < x[par_indx[m+1:]].min():  # (m+1)th < (m+2)th
			return list(par_indx[:m+1])
		else:  # mth = (m+1)th = (m+2)th, so increment and recurse
			return self._msmallest_with_ties(x, m+2)


	def _make_matches(self, X, X_m, W, m):

		"""
		Performs nearest-neigborhood matching using specified weighting
		matrix in measuring distance. Ties are included, so the number
		of matches for a given unit can be greater than m.

		Expected args
		-------------
			X: matrix, ndarray
				Observations to find matches for.
			X_m: matrix, ndarray
				Pool of potential matches.
			W: string or matrix, ndarray
				Weighting matrix to be used in norm calcuation. Acceptable
				values are string 'inv' for inverse variance weighting,
				or any arbitrary K-by-K matrix.
			m: integer
				The number of units to match to a given subject.

		Returns
		-------
			List of matched indices.
		"""

		m_indx = [None] * X.shape[0]

		for i in xrange(X.shape[0]):
			m_indx[i] = self._msmallest_with_ties(self._norm(X_m-X[i], W), m)

		return m_indx


	def _bias(self, m_indx, Y_m, X_m, X):

		"""
		Estimates bias resulting from imperfect matches using least squares.
		When estimating ATT, regression should use control units. When
		estimating ATC, regression should use treated units.

		Expected args
		-------------
			m_indx: list
				Index of indices of matched units.
			Y_m: array-like
				Vector of outcomes to regress.
			X_m: matrix, ndarray
				Covariate matrix to regress on.
			X: matrix, ndarray
				Covariate matrix of subjects under study.

		Returns
		-------
			Vector of estimated biases.
		"""

		flat_indx = list(chain.from_iterable(m_indx))

		X_m1 = np.empty((len(flat_indx), X_m.shape[1]+1))
		X_m1[:,0] = 1
		X_m1[:,1:] = X_m[flat_indx]
		beta = np.linalg.lstsq(X_m1, Y_m[flat_indx])[0]

		return [np.dot(X[i]-X_m[m_indx[i]].mean(0), beta[1:]) for i in xrange(X.shape[0])]
	

	def matching(self, wmat='inv', m=1, xbias=False):

		"""
		Estimates average treatment effects using matching with replacement.

		By default, the weighting matrix used in measuring distance is the
		inverse variance matrix. The Mahalanobis metric or other arbitrary
		weighting matrices can also be used instead.

		The number of matches per subject can also be specified. Ties entries
		are included, so the number of matches can be greater than specified
		for some subjects.

		Bias correction can optionally be done. For treated units, the bias
		resulting from imperfect matches is estimated by
			(X_t - X_c[matched]) * b,
		where b is the estimated coefficient from regressiong Y_c[matched] on
		X_c[matched]. For control units, the analogous procedure is used.
		For details, see Imbens and Rubin.

		Expected args
		-------------
			wmat: string or matrix, ndarray
				Weighting matrix to be used in norm calcuation. Acceptable
				values are None	(inverse variance, default), string 'maha'
				for Mahalanobis	metric, or any arbitrary k-by-k matrix.
			m: integer
				The number of units to match to a given subject. Defaults
				to 1.
			xbias: Boolean
				Correct bias resulting from imperfect matches or not; defaults
				to no correction.
		"""

		if wmat == 'maha':
			wmat = np.linalg.inv(np.cov(self.X, rowvar=False))

		m_indx_t = self._make_matches(self.X_t, self.X_c, wmat, m)
		m_indx_c = self._make_matches(self.X_c, self.X_t, wmat, m)

		self.ITT = np.empty(self.N)
		self.ITT[self.D==1] = self.Y_t - [self.Y_c[m_indx_t[i]].mean() for i in xrange(self.N_t)]
		self.ITT[self.D==0] = [self.Y_t[m_indx_c[i]].mean() for i in xrange(self.N_c)] - self.Y_c

		if xbias:
			self.ITT[self.D==1] -= self._bias(m_indx_t, self.Y_c, self.X_c, self.X_t)
			self.ITT[self.D==0] += self._bias(m_indx_c, self.Y_t, self.X_t, self.X_c)

		self.ATE = self.ITT.mean()
		self.ATT = self.ITT[self.D==1].mean()
		self.ATC = self.ITT[self.D==0].mean()


	def weighting(self):

		"""
		Computes ATE using inverse probability weighted estimator as in missing data.
		"""

		self._check_prereq('pscore')
		p_t, p_c = 1/self.pscore['fitted'][self.D==1], 1/(1-self.pscore['fitted'][self.D==0])
		self.ATE = (self.Y_t*p_t).sum()/p_t.sum() - (self.Y_c*p_c).sum()/p_c.sum()


	def _ols_predict(self, Y, X, X_new, const=1):

		"""
		Estimates linear regression model with least squares and project based
		on new input data.

		Expected args
		-------------
			Y: array-like
				Vector of observed outcomes.
			X: matrix, ndarray
				Matrix of covariates to regress on.
			X_new: matrix, ndarray
				Matrix of covariates used to generate predictions.

		Returns
		-------
			Vector of predicted values.
		"""

		X1 = np.empty((X.shape[0], X.shape[1]+1))
		X1[:, 0] = 1
		X1[:, 1:] = X
		beta = np.linalg.lstsq(X1, Y)[0]

		return beta[0]*const + X_new.dot(beta[1:])


	def ols(self):

		"""
		Estimates average treatment effects using least squares.
		"""

		self.ITT = np.empty(self.N)
		self.ITT[self.D==1] = self.Y_t - self._ols_predict(self.Y_c, self.X_c, self.X_t)
		self.ITT[self.D==0] = self._ols_predict(self.Y_t, self.X_t, self.X_c) - self.Y_c

		self.ATE = self.ITT.mean()
		self.ATT = self.ITT[self.D==1].mean()
		self.ATC = self.ITT[self.D==0].mean()


class Results(object):


	def __init__(self, causal):

		self.causal = causal


	def normalized_difference(self):

		print self.causal.ndiff


	def propensity_score(self):

		if not self.causal.pscore:
			self.causal.compute_propensity_score()

		print 'Coefficients:', self.causal.pscore['coeff']
		print 'Log-likelihood:', self.causal.pscore['loglike']


from scipy.stats import norm
class parameters(object):

	"""
	Class object that stores the parameter values for use in the baseline model.
	
	See SimulateData function for model description.

	Args:
		N = Sample size (control + treated units) to generate
		k = Number of covariates
	"""

	def __init__(self, N=500, k=3):  # set initial parameter values
		self.N = N  # sample size (control + treated units)
		self.k = k  # number of covariates

		self.delta = 3
		self.beta = np.ones(k)
		self.theta = np.ones(k)
		self.mu = np.zeros(k)
		self.Sigma = np.identity(k)
		self.Gamma = np.identity(2)


def SimulateData(para=parameters(), nonlinear=False, return_counterfactual=False):

	"""
	Function that generates data according to one of two simple models that
	satisfies the Unconfoundedness assumption.

	The covariates and error terms are generated according to
		X ~ N(mu, Sigma), epsilon ~ N(0, Gamma).
	The counterfactual outcomes are generated by
		Y_0 = X*beta + epsilon_0,
		Y_1 = delta + X*(beta+theta) + epsilon_1,
	if the nonlinear Boolean is False, or by
		Y_0 = sum_of_abs(X) + epsilon_0,
		Y_1 = sum_of_squares(X) + epsilon_1,
	if the nonlinear Boolean is True.

	Selection is done according to the following propensity score function:
		P(D=1|X) = Phi(X*beta),
	where Phi is the standard normal CDF.

	Args:
		para = Model parameter values supplied by parameter class object
		nonlinear = Boolean indicating whether the data generating model should
		            be linear or not
		return_counterfactual = Boolean indicating whether to return vectors of
		                        counterfactual outcomes

	Returns:
		Y = N-dimensional array of observed outcomes
		D = N-dimensional array of treatment indicator; 1=treated, 0=control
		X = N-by-k matrix of covariates
		Y_0 = N-dimensional array of non-treated outcomes
		Y_1 = N-dimensional array of treated outcomes
	"""

	k = len(para.mu)

	X = np.random.multivariate_normal(mean=para.mu, cov=para.Sigma,
	                                  size=para.N)
	epsilon = np.random.multivariate_normal(mean=np.zeros(2), cov=para.Gamma,
	                                        size=para.N)

	Xbeta = np.dot(X, para.beta)

	pscore = norm.cdf(Xbeta)
	# for each p in pscore, generate Bernoulli rv with success probability p
	D = np.array([np.random.binomial(1, p, size=1) for p in pscore], dtype='bool').flatten()

	if nonlinear:
		Y_0 = abs(X).sum(1) + epsilon[:, 0]
		Y_1 = (X**2).sum(1) + epsilon[:, 1]
	else:
		Y_0 = Xbeta + epsilon[:, 0]
		Y_1 = para.delta + np.dot(X, para.beta+para.theta) + epsilon[:, 1]

	Y = (1 - D) * Y_0 + D * Y_1  # compute observed outcome

	if return_counterfactual:
		return Y, D, X, Y_0, Y_1
	else:
		return Y, D, X


def Lalonde():

	import pandas as pd

	lalonde = pd.read_csv('ldw_exper.csv')

	covariate_list = ['black', 'hisp', 'age', 'married', 
	                  'educ', 're74', 'u74', 're75', 'u75']

	# don't know how to not convert to array first
	return np.array(lalonde['re78']), np.array(lalonde['t']), np.array(lalonde[covariate_list])


Y, D, X, Y0, Y1 = SimulateData(para=parameters(N=5000, k=3), return_counterfactual=True)
causal = CausalModel(Y, D, X)
display = Results(causal)
