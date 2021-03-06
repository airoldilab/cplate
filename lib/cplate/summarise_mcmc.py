import collections
import gc
import itertools
import os
import sys
import tarfile

import numpy as np
from numpy.lib import recfunctions as nprf
from scipy.stats import mstats

import libio

#==============================================================================
# General-purpose MCMC diagnostic and summarization functions
#==============================================================================

def compute_n_large(x, p_threshold, axis=None):
    '''
    Compute number of large positions per draw (along axes of array).

    Parameters
    ----------
    x : array_like
        Array or array_like object of draws
    p_threshold : float or iterable of floats
        Threshold(s) for large positions as fraction of total
    axis : integer, optional
        Axis along which n_large is computed. The default is to flatten x.

    Returns
    -------
    n_large : ndarray
        A new array containing the values of n_large. Has one row per value of
        p_threshold provided.
    '''
    # Compute normalized version of x
    x = np.asarray(x)
    s_x = np.sum(x,axis=axis)[:,np.newaxis]
    p_x = x / s_x
    
    if not isinstance(p_threshold, collections.Iterable):
        p_threshold = (p_threshold,)

    n_large = np.empty((len(p_threshold), np.size(s_x)), dtype=int)
    for i, p_i in enumerate(p_threshold):
        n_large[i] = np.sum(p_x > p_i, axis=axis)

    return n_large

def mean_abs_dev(x, w=None, axis=None):
    '''
    Compute mean absolute deviation along axes of an array

    Parameters
    ----------
    x : array_like
        Array or array_like object to compute MAD of
    w : array_like, optional
        Optional vector of weights, broadcastable with x
    axis : integer, optional
        Axis along which mean absolute deviations are computed. The default is
        to flatten x.

    Returns
    -------
    mad : ndarray
        A new array containing the mean absolute deviation values
    '''
    x = np.asarray(x)
    if w is not None:
        w = np.asarray(w)
    else:
        w = 1
    
    if axis == 0 or axis is None or x.ndim <= 1:
        return np.sum(w*np.abs(x - np.sum(x*w, axis)), axis=axis)
    
    ind = [slice(None)] * x.ndim
    ind[axis] = np.newaxis
    
    return np.sum(w*np.abs(x - np.sum(x*w, axis)[ind]), axis=axis)

def localization_index(x, p, axis=None):
    r'''
    Compute localization index for given region

    This normalizes the MADs by the MAD of a uniform distribution with the same
    support, then subtracts this from 1. Mathematically, it is defined as
    $$L = 1 - \frac{MAD}{n/4}\ ,$$
    where $n$ is the length of the cluster region.
    It is _not_ bounded between 0 and 1 (the maximum is 1 for a spike, the
    minimum is -1 for two equal-weighted spikes at the region's boundary), but
    it does provide a useful reference point.
    
    Parameters
    ----------
    x : array_like
        Array or array_like object to compute localization index from
    p : array_like
        Array of probabilities, broadcastable with x
    axis : integer, optional
        Axis along which localization indices are computed. The default is
        to flatten x.

    Returns
    -------
    L : ndarray
        A new array containing the values of the localization index.
    '''
    m = mean_abs_dev(x=x, w=p, axis=axis)
    n = max(x.size, p.size) / m.size
    return 1. - m / (n/4.)

def entropy(p, axis=None):
    '''
    Compute entropy along axes of an array

    Parameters
    ----------
    p : array_like
        Array or array_like object containing PMFs to compute entropy from.
    axis : integer, optional
        Axis along which entropies are computed. The default is to flatten p.
        Note p.sum(axis) should be 1.

    Returns
    -------
    e : ndarray
        A new array containing the entropies
    '''
    p = np.asarray(p)
    lp = np.log2(p)
    lp[~np.isfinite(lp)] = 0.
    
    if axis == 0 or axis is None or p.ndim <= 1:
        return np.sum(-p*lp, axis=axis)
    
    ind = [slice(None)] * p.ndim
    ind[axis] = np.newaxis
    
    return np.sum(-p*lp, axis=axis)

def structure_index(x, axis=None):
    r'''
    Compute structure index along axes of an array

    Whereas the MAD-based index measures localization as spread from the
    cluster's center, this entropy-based index measures structure more
    generally. Entropy is minimized for a single spike and maximized for a
    uniform. This seems like reasonable behavior for our purposes. I'm calling
    this the **structure index**, computed as
    $$ S = 1 - \frac{E}{\log(n)}\ , $$
    where $E$ is the entropy of the distribution (given by x)
    within each cluster and $n$ is the cluster's length. $E$ is calculated as
    $$ E = \frac{1}{\sum_i \beta_i} \sum_i - \beta_i
    \log\left(\frac{\beta_i}{\sum_i \beta_i}\right)\ .$$

    Parameters
    ----------
    x : array_like
        Array or array_like object containing regions for which the structure
        index will be computed.
    axis : integer, optional
        Axis along which structure indices are computed. The default is to
        flatten p.  Note p.sum(axis) should be 1.

    Returns
    -------
    s : ndarray
        A new array containing the structure indices
    '''
    p = x / np.sum(x,axis=axis)[:,np.newaxis]
    E = entropy(p, axis=axis)
    return 1. - E / np.log2(x.size / E.size)

def sparsity_index(x, q, axis=None):
    r'''
    Compute sparsity index along axes of an array

    This index provides a direct measure of the sparsity of a the distribution
    of nucleosome positions within a given structure. It has one scientific
    parameter q, used as follows:
        Let $n_q$ be the number of positions within a given cluster needed to
        capture $q$ of that cluster's expected reads. Then, the sparsity index
        is computed as
        $$R = 1 - \frac{n_q}{q (2w + 1)} \ ,$$
        where the cluster is 2w + 1 positions wide.
    This is normalized like the other indices to be 1 for a single spike and 0
    for a locally uniform distribution of nucleosome positions.

    Parameters
    ----------
    x : array_like
        Array or array_like object containing regions for which the sparsity
        index will be computed.
    q : float
        Quantile of expected read distribution (across positions) to compute
    axis : integer, optional
        Axis along which sparsity indices are computed. The default is to
        flatten x.

    Returns
    -------
    s : ndarray
        A new array containing the structure indices
    '''
    s_x = np.sum(x,axis=axis)[:,np.newaxis]
    p_x = -x / s_x
    p_x.sort(axis=axis)
    p_x = np.cumsum(-p_x, axis=axis)
    
    if not isinstance(q, np.ndarray):
        q = np.array(q)

    n_q = np.empty((len(q), np.size(s_x)), dtype=int)

    for i, q_i in enumerate(q):
        n_q[i] = np.apply_along_axis(np.searchsorted, axis, p_x, q_i)

    return (1. - n_q.T * 1. / (q * x.shape[1])).T

def gaussian_window(h=80, sigma=20.):
    '''
    Builds a normalized Gaussian window

    Parameters
    ----------
    - h : int
        Integer half-width for window.
    - sigma : float
        Standard deviation for Gaussian window.

    Returns
    -------
    - w : float np.ndarray
        Array of length 2*h + 1 containing window.
    '''
    # Build Gaussian window
    w = np.exp(-np.arange(-h,h+1)**2/2./sigma**2)
    w /= w.sum()
    return w

def effective_sample_sizes(**kwargs):
    '''
    Estimate effective sample size for each input using AR(1) approximation.
    Each input should be a 1- or 2-dimensional ndarray. 2-dimensional inputs
    should have one variable per column, one iteration per row.

    Parameters
    ----------

    Returns
    -------
        - If only one array of draws is provided, a single array containing the
          effective sample size(s) for those variables.
        - If multiple arrays are provided, a dictionary with keys identical to
          those provided as parameters and one array per input containing
          effective sample size(s).

    '''
    # Ensure that at least one input was provided
    if len(kwargs) < 1:
        return ValueError('Must provide at least one array of draws.')

    # Allocate empty dictionary for results
    ess = {}

    # Iterate over arrays of draws
    for var, draws in kwargs.iteritems():
        # Add dimension to 1d arrays
        if len(np.shape(draws)) < 2:
            draws = draws[:,np.newaxis]

        # Demean the draws
        z = (draws - np.mean(draws, axis=0)) / np.std(draws, axis=0)

        # Compute lag-1 autocorrelation by column
        rho = np.mean(z[1:] * z[:-1], axis=0)

        # Compute ess from lag-1 autocorrelation
        ess[var] = np.shape(draws)[0]*(1. - rho)/(1. + rho)

    if len(kwargs) > 1:
        return ess
    else:
        return ess[kwargs.keys()[0]]

def ess1d(x):
    '''
    Estimate effective sample size of input using AR(1) approximation.

    Parameters
    ----------
    x : 1d ndarray
        Unidimensional array of draws

    Returns
    -------
    ess : float
        The estimated effective sample size of the given array.
    '''
    # Compute lag-1 autocorrelation by column
    z = (x - x.mean()) / x.std()
    rho = np.mean(z[1:] * z[:-1])

    # Compute ess from lag-1 autocorrelation
    ess = x.size * (1. - rho) / (1. + rho)

    return ess

def posterior_means(**kwargs):
    '''
    Estimate posterior means from inputs.
    Each input should be a 1- or 2-dimensional ndarray. 2-dimensional inputs
    should have one variable per column, one iteration per row.

    Parameters
    ----------
        - **kwargs
            Names and arrays of MCMC draws.

    Returns
    -------
        - If only one array of draws is provided, a single array containing the
          posterior mean estimate(s) for those variables.
        - If multiple arrays are provided, a dictionary with keys identical to
          those provided as parameters and one array per input containing
          posterior mean estimate(s).

    '''
    # Ensure that at least one input was provided
    if len(kwargs) < 1:
        return ValueError('Must provide at least one array of draws.')

    # Allocate empty dictionary for results
    means = {}

    # Iterate over arrays of draws
    for var, draws in kwargs.iteritems():
        # Add dimension to 1d arrays
        if len(np.shape(draws)) < 2:
            draws = draws[:,np.newaxis]

        # Estimate posterior means
        means[var] = np.mean(draws, 0)

    if len(kwargs) > 1:
        return means
    else:
        return means[kwargs.keys()[0]]

def posterior_variances(**kwargs):
    '''
    Estimate posterior variances from inputs.
    Each input should be a 1- or 2-dimensional ndarray. 2-dimensional inputs
    should have one variable per column, one iteration per row.

    Parameters
    ----------
        - **kwargs
            Names and arrays of MCMC draws.

    Returns
    -------
        - If only one array of draws is provided, a single array containing the
          posterior variance estimate(s) for those variables.
        - If multiple arrays are provided, a dictionary with keys identical to
          those provided as parameters and one array per input containing
          posterior variance estimate(s).

    '''
    # Ensure that at least one input was provided
    if len(kwargs) < 1:
        return ValueError('Must provide at least one array of draws.')

    # Allocate empty dictionary for results
    variances = {}

    # Iterate over arrays of draws
    for var, draws in kwargs.iteritems():
        # Add dimension to 1d arrays
        if len(np.shape(draws)) < 2:
            draws = draws[:,np.newaxis]

        # Estimate posterior variances
        variances[var] = np.var(draws, 0)

    if len(kwargs) > 1:
        return variances
    else:
        return variances[kwargs.keys()[0]]

def posterior_stderrors(**kwargs):
    '''
    Estimate posterior standard errors from inputs.
    Each input should be a 1- or 2-dimensional ndarray. 2-dimensional inputs
    should have one variable per column, one iteration per row.

    Parameters
    ----------
        - **kwargs
            Names and arrays of MCMC draws.

    Returns
    -------
        - If only one array of draws is provided, a single array containing the
          posterior standard error estimate(s) for those variables.
        - If multiple arrays are provided, a dictionary with keys identical to
          those provided as parameters and one array per input containing
          posterior standard error estimate(s).

    '''
    # Ensure that at least one input was provided
    if len(kwargs) < 1:
        return ValueError('Must provide at least one array of draws.')

    # Allocate empty dictionary for results
    stderrors = {}

    # Iterate over arrays of draws
    for var, draws in kwargs.iteritems():
        # Add dimension to 1d arrays
        if len(np.shape(draws)) < 2:
            draws = draws[:,np.newaxis]

        # Estimate posterior means
        stderrors[var] = np.std(draws, 0)

    if len(kwargs) > 1:
        return stderrors
    else:
        return stderrors[kwargs.keys()[0]]

def find_maxima(x, boundary=False):
    '''
    Finds local maxima in sequence x, defining local maxima simply by
    low-high-low triplets.

    Parameters
    ----------
    - x : ndarray
        Sequence of values to search for local maxima
    - boundary : bool
        If True, include boundaries as possible maxima

    Returns
    -------
    - maxima : ndarray
        Boolean array of the same size as x with local maxima True

    '''
    # Intialization
    up, down    = np.ones((2, x.size), dtype=int)
    # Central cases
    up[1:-1]    = (x[1:-1]>x[:-2])
    down[1:-1]  = (x[2:]<x[1:-1])

    if boundary:
        # Boundary cases
        down[0]     = (x[1]<x[0])
        up[-1]      = (x[-1]>x[-2])
    # Logical and
    maxima  = up*down
    return maxima

def local_relative_occupancy(b_t, window_small, window_local):
    '''
    Compute local relative occupancy from vector of coefficients.

    Parameters
    ----------
    - b_t : ndarray
        Array of coefficients from a single draw
    - window_small : ndarray
        Array containing small window for local relative occupancy
    - window_local : ndarray
        Array containing larger window for local relative occupancy

    Returns
    -------
    - l : ndarray
        Array of same size as b_t containing local relative occupancies

    '''
    return (np.convolve(b_t, window_small, 'same') /
            np.convolve(b_t, window_local, 'same'))

def condense_detections(detections):
    '''
    Condense adjacent detections (from smoothed local occupancy) into centers
    and number of adjacent detections.

    Parameters
    ----------
    - detections : ndarray
        1d array of detected positions

    Returns
    -------
    - detections : ndarray
        1d array of detected centers
    - n : integer ndarray
        Number of detections per center
    '''
    x = detections.copy() + 0.
    n = np.ones_like(x)

    while np.any(np.diff(x) < 2):
        first = np.min(np.where(np.diff(x)<2)[0])
        last = first + np.min(np.r_[np.where(np.diff(x[first:]) > 1)[0],
                                    x[first:].size - 1]) + 1
        x *= n
        x = np.r_[x[:first], np.sum(x[first:last]), x[last:]]
        n = np.r_[n[:first], np.sum(n[first:last]), n[last:]]
        x /= n

    return x, n

def greedy_maxima_search(x, min_spacing=100, remove_boundary=1, verbose=0):
    '''
    Greedy search for local maxima in sequence subject to minimum spacing
    constraint.

    Parameters
    ----------
    - x : ndarray
        1d sequence of values to search for local maxima
    - min_spacing : int
        Minimum spacing of positions. Greedy search continues until this
        constraint is met.
    - remove_boundary : int
        Length of region to exclude at each end of the sequence. 
    - verbose : int
        Level of verbosity in output

    Returns
    -------
    - out : ndarray
        Integer array of same shape as x containing ones at positions found in
        greedy search and zeros everywhere else.
    '''
    # Find local maxima in sequence; need indices of maxima, not binary
    # indicators
    candidates = np.where(find_maxima(x))[0]

    if remove_boundary > 0:
        # Exclude boundary positions
        candidates = candidates[candidates >= remove_boundary]
        candidates = candidates[candidates < x.size - remove_boundary]

    # Order positions by score, descending
    candidates[:] = candidates[np.argsort(x[candidates])]
    candidates = candidates[::-1]
    
    # Initialize vector for identified peaks
    peaks = np.zeros(np.size(x) / min_spacing + 2, dtype=int)
    peaks[0] = candidates[0]
    n_peaks = 1

    if np.size(candidates) > 1:
        for candidate in candidates:
            # Compute minimal distance between candidate and existing peaks
            distance_candidate = np.min(np.abs(candidate - peaks[:n_peaks]))

            # Skip if distance is less than constraint
            if distance_candidate <= min_spacing:
                continue

            # Otherwise, add candidate to the peak list
            peaks[n_peaks] = candidate
            n_peaks += 1

    peaks = peaks[:n_peaks]
    peaks.sort()
    return peaks

def maxima_search(x, min_spacing=100, remove_boundary=1, verbose=0):
    '''
    Search for local maxima in sequence subject to minimum spacing constraint.

    Parameters
    ----------
    - x : ndarray
        1d sequence of values to search for local maxima
    - min_spacing : int
        Minimum spacing of positions. Greedy search continues until this
        constraint is met.
    - remove_boundary : int
        Length of region to exclude at each end of the sequence. 
    - verbose : int
        Level of verbosity in output

    Returns
    -------
    - out : ndarray
        Integer array of same shape as x containing ones at positions found in
        greedy search and zeros everywhere else.
    '''
    # Find local maxima in sequence; need indices of maxima, not binary
    # indicators
    positions       = np.where(find_maxima(x))[0]

    if remove_boundary > 0:
        # Exclude boundary positions
        positions = positions[positions>=remove_boundary]
        positions = positions[positions<x.size-remove_boundary]

    # Get spacing
    spacing         = np.diff(positions)

    # Check for bad overlaps
    while spacing.size > 0 and spacing.min() < min_spacing:
        # Save positions from previous iterations
        positions_last = positions.copy()

        # Find bad positions
        bad = np.where(spacing < min_spacing)[0]

        # Find first bad position
        first_bad    = np.min(bad)

        # Find which positions overlap with given position

        # First, get where overlaps below threshold are located
        good    = np.where(spacing >= min_spacing)[0]

        # Get number of positions from top bad one to good ones
        dist    = first_bad - good

        # Find limits of bad cluster
        if np.any(dist<0):
            last_in_cluster   = good[dist<0][np.argmax(dist[dist<0])]
            last_in_cluster   = min(last_in_cluster+1, spacing.size+1)
        else:
            last_in_cluster   = spacing.size+1

        if np.any(dist>0):
            first_in_cluster  = good[dist>0][np.argmin(dist[dist>0])]
            first_in_cluster  = max(0,first_in_cluster+1)
        else:
            first_in_cluster  = 0

        # Check coefficients of positions in cluster for maximum
        top_in_cluster    = np.argmax(x[positions[first_in_cluster:
                                                  last_in_cluster]])
        top_in_cluster    = first_in_cluster + top_in_cluster

        # Handle non-uniqueness
        top_in_cluster    = np.min(top_in_cluster)

        # Eliminate bad neighbors from positions
        keep    = np.ones(positions.size, dtype=bool)

        if top_in_cluster > 0:
            space = (positions[top_in_cluster] - positions[top_in_cluster-1])
            if space < min_spacing:
                keep[top_in_cluster-1] = False

        if top_in_cluster < positions.size-1:
            space = (positions[top_in_cluster+1] - positions[top_in_cluster])
            if space < min_spacing:
                keep[top_in_cluster+1] = False

        positions       = positions[keep]

        if positions.size == positions_last.size:
            print >> sys.stderr, 'Error --- greedy search is stuck'
            print >> sys.stderr, positions, spacing
            break

        if verbose:
            print >> sys.stderr, positions, spacing

        # Update spacing
        spacing         = np.diff(positions)

    return positions

def get_cluster_centers(x, window, min_spacing, edge_correction=True):
    '''
    Find cluster centers via Parzen smoothing and greedy search

    Parameters
    ----------
    x : array_like
        Array (1d) containing sequence to be smoothed and clustered
    window : array_like
        Array (1d) containing window for Parzen smoothing
    min_spacing : int
        Minimum spacing for greedy local maximum search
    edge_correction : bool, optional
        Correct for edge effects in Parzen smoothing? True is analogous to local
        mean, False is analogous to local sum

    Returns
    -------
    centers : integer ndarray
        A new (1d) ndarray containing the cluster centers. Its length is the
        number of cluster centers, and each entry is a position. This is _not_
        indicator notation.
    '''
    # Set baseline for edge correction
    if edge_correction:
        baseline = np.convolve(np.ones_like(x), window, 'same')
    else:
        baseline = 1.

    # Parzen window smoothing of sequence
    s = np.convolve(x, window, 'same')/baseline
    
    # Identify maxima
    clusters = greedy_maxima_search(s, min_spacing=min_spacing,
                                    remove_boundary=min_spacing/2)

    # Return their locations, not indicators
    return clusters

def summarise(cfg, chrom=1, null=False, mmap=False, detect_fmt=("%.1f", "%d")):
    '''
    Coordinate summarisation of MCMC results.

    Parameters
    ----------
    - cfg : dictionary
        Dictionary of parameters containing at least those relevant MCMC
        draw and summary output paths and parameters for summarization.
    - chrom : int
        Index of chromosome to analyze
    - null : bool
        Summarise null results?

    Returns
    -------
    - status : int
        Integer status for summarisation. 0 for success, > 0 for failure.
    '''
    # Reference useful information in local namespace
    n_burnin    = cfg['mcmc_params']['n_burnin']
    scratch     = cfg['mcmc_summaries']['path_scratch']
    width_local = cfg['mcmc_summaries']['width_local']
    p_detect    = cfg['mcmc_summaries']['p_detect']
    bp_per_nucleosome = cfg['mcmc_summaries']['bp_per_nucleosome']
    
    # Extract window size information (+/-) from config
    concentration_pm = cfg['mcmc_summaries']['concentration_pm']
    if isinstance(concentration_pm, str):
        pm_list = [int(s) for s in concentration_pm.split(',')]
    else:
        pm_list = [concentration_pm]
    
    # Check for existence and writeability of scratch directory
    if os.access(scratch, os.F_OK):
        # It exists, check for read-write
        if not os.access(scratch, os.R_OK | os.W_OK):
            print >> sys.stderr, ("Error --- Cannot read and write to %s" %
                                  scratch)
            return 1
    else:
        # Otherwise, try to make the directory
        os.makedirs(scratch)

    # Extract results to scratch directory
    if null:
        pattern_results = cfg['mcmc_output']['null_out_pattern']
    else:
        pattern_results = cfg['mcmc_output']['out_pattern']
    pattern_results = pattern_results.strip()
    path_results = pattern_results.format(**cfg) % chrom
    
    archive = tarfile.open(name=path_results, mode='r:*')
    archive.extractall(path=scratch)
    names_npy = archive.getnames()
    archive.close()

    # Load results of interest
    if mmap:
        mmap_mode = 'r+'
    else:
        mmap_mode = None

    theta   = np.load(scratch + '/theta.npy', mmap_mode=mmap_mode)
    mu      = np.load(scratch + '/mu.npy')

    # Remove burnin
    if n_burnin > 0:
        mu = mu[n_burnin:]
        theta = theta[n_burnin:]

    # Compute effective sample sizes
    n_eff = np.array([ess1d(theta_k) for theta_k in theta.T])
    gc.collect()
    
    # Compute concentration summaries
    local_concentrations = collections.OrderedDict()
    global_concentrations = collections.OrderedDict()

    # Iteration over concentration window sizes (+/-)
    for pm in pm_list:
        # Estimate probability of +/-(pm) local concentrations
        window_local = np.ones(width_local)
        window_pm    = np.ones(1 + 2*pm)
        baseline = (np.convolve(np.ones_like(theta[0]), window_pm, 'same') /
                    np.convolve(np.ones_like(theta[0]), window_local, 'same'))
        
        # Setup array for estimates by basepair
        p_local_concentration = np.zeros(theta.shape[1], dtype=np.float)
        
        # Iterate over draws
        mean_lro = np.zeros(theta.shape[1], dtype=np.float)
        se_lro = np.zeros(theta.shape[1], dtype=np.float)
        for t in xrange(theta.shape[0]):
            bt = np.exp(theta[t])
            local_occupancy_smoothed = local_relative_occupancy(
                bt, window_pm, window_local)
            delta = local_occupancy_smoothed - mean_lro
            mean_lro += delta / (t+1.)
            se_lro += delta * (local_occupancy_smoothed - mean_lro)
            p_local_concentration *= t/(t+1.)
            p_local_concentration += (
                (local_occupancy_smoothed > baseline)/(t+1.))
        se_lro = np.sqrt(se_lro / (theta.shape[0] - 1))
        
        # Store results in dictionary
        key = 'p_local_concentration_pm%d' % pm
        local_concentrations[key] = p_local_concentration
        key = 'mean_local_concentration_pm%d' % pm
        local_concentrations[key] = mean_lro
        key = 'se_local_concentration_pm%d' % pm
        local_concentrations[key] = se_lro
        key = 'z_local_concentration_pm%d' % pm
        local_concentrations[key] = mean_lro / se_lro

        # Clean-up
        del local_occupancy_smoothed
        gc.collect()
        
        # Posterior quantiles for global concentrations
        baseline_global = (np.array([np.sum(np.exp(theta_t)) for theta_t in
                                     theta]) / theta.shape[1]
                            * bp_per_nucleosome)
        
        # Setup arrays for means and quantiles by basepair
        q_global_concentration = np.zeros(theta.shape[1], dtype=np.float)
        mean_global_concentration = np.zeros(theta.shape[1], dtype=np.float)
        
        # Iterate over basepairs
        for bp in xrange(theta.shape[1]):
            w = slice(max(0,bp-pm), min(bp+pm+1, theta.shape[1]))
            prop = (np.sum(np.exp(theta[:,w]), 1) / baseline_global /
                    (w.stop-w.start))
            mean_global_concentration[bp] = np.mean(prop)
            q_global_concentration[bp] =  mstats.mquantiles(prop, 1.-p_detect)

        # Store results in dictionaries
        key = 'q_global_concentration_pm%d' % pm
        global_concentrations[key] = q_global_concentration
        key = 'mean_global_concentration_pm%d' % pm
        global_concentrations[key] = mean_global_concentration
    
    # Compute posterior means
    theta_postmean = np.mean(theta, 0)
    b_postmean = np.array([np.mean(np.exp(theta_k)) for theta_k in theta.T])

    # Compute standard errors
    theta_se = np.array([np.std(theta_k) for theta_k in theta.T])
    b_se = np.array([np.std(np.exp(theta_k)) for theta_k in theta.T])

    # Compute posterior medians
    theta_postmed = np.array([np.median(theta_k) for theta_k in theta.T])
    b_postmed = np.exp(theta_postmed)
    
    # Provide nicely-formatted delimited output for analyses and plotting
    if null:
        pattern_summaries = cfg['mcmc_output']['null_summary_pattern']
    else:
        pattern_summaries = cfg['mcmc_output']['summary_pattern']
    pattern_summaries = pattern_summaries.strip()
    path_summaries = pattern_summaries.format(**cfg) % chrom

    # Build recarray of summaries, starting with coefficients and diagnostics
    summaries = np.rec.fromarrays([theta_postmean, theta_postmed, theta_se,
                                   b_postmean, b_postmed, b_se, n_eff],
                                  names=('theta', 'theta_med', 'se_theta', 'b',
                                         'b_med', 'se_b', 'n_eff',))

    # Append local concentration information
    summaries = nprf.append_fields(base=summaries,
                                   names=local_concentrations.keys(),
                                   data=local_concentrations.values())
    
    # Append global concentration information
    summaries = nprf.append_fields(base=summaries,
                                   names=global_concentrations.keys(),
                                   data=global_concentrations.values())
    
    # Write summaries to delimited text file
    libio.write_recarray_to_file(fname=path_summaries, data=summaries,
                                 header=True, sep=' ')

    # Run detection, if requested
    if p_detect is not None and not null:
        for pm in pm_list:
            # Find detected positions
            key = 'p_local_concentration_pm%d' % pm
            detected = np.where(local_concentrations[key] > p_detect)[0]

            # Condense regions
            detected, n = condense_detections(detected)

            # Write detections to text file
            pattern_detections = cfg['mcmc_output']['detections_pattern']
            pattern_detections = pattern_detections.strip()
            path_detections = pattern_detections.format(**cfg) % (chrom, pm)

            detections = np.rec.fromarrays([detected, n],
                                           names=('pos', 'n'))
            libio.write_recarray_to_file(fname=path_detections, data=detections,
                                         header=True, sep=' ', fmt=detect_fmt)

    # Clean-up scratch directory
    for name in names_npy:
        os.remove(scratch + '/' + name)

    return 0

def summarise_clusters(cfg, chrom=1, null=False):
    '''
    Coordinate summarisation of MCMC results by cluster.

    Clusters are defined via Parzen window smoothing with a cfg-specified
    bandwidth and minimum separation. Following clustering, all cluster-level
    summaries are computed within each iteration (localization, structure,
    occupancy, etc.). The reported outputs are posterior summaries of these
    cluster-level summaries (mean, SD, etc.).

    Parameters
    ----------
    - cfg : dictionary
        Dictionary of parameters containing at least those relevant MCMC
        draw and summary output paths and parameters for summarization.
    - chrom : int
        Index of chromosome to analyze
    - null : bool
        Summarise null results?

    Returns
    -------
    - status : int
        Integer status for summarisation. 0 for success, > 0 for failure.
    '''
    # Reference useful information in local namespace
    n_burnin    = cfg['mcmc_params']['n_burnin']
    scratch     = cfg['mcmc_summaries']['path_scratch']
    # Cluster-level summary information
    cluster_min_spacing = cfg['mcmc_summaries']['cluster_min_spacing']
    cluster_bw = cfg['mcmc_summaries']['cluster_bw']
    cluster_width = cfg['mcmc_summaries']['cluster_width']
    h = cluster_width/2

    # Extract q_sparsity information for n_large summaries from config
    q_sparsity = cfg['mcmc_summaries']['q_sparsity']
    if isinstance(q_sparsity, str):
        q_sparsity = [float(s) for s in q_sparsity.split(',')]
    else:
        q_sparsity = [q_sparsity]

    # Extract p_threshold information for n_large summaries from config
    p_threshold = cfg['mcmc_summaries']['p_threshold']
    if isinstance(p_threshold, str):
        p_threshold = [float(s) for s in p_threshold.split(',')]
    else:
        p_threshold = [p_threshold]
    
    # Check for existence and writeability of scratch directory
    if os.access(scratch, os.F_OK):
        # It exists, check for read-write
        if not os.access(scratch, os.R_OK | os.W_OK):
            print >> sys.stderr, ("Error --- Cannot read and write to %s" %
                                  scratch)
            return 1
    else:
        # Otherwise, try to make the directory
        os.makedirs(scratch)

    # Extract results to scratch directory
    if null:
        pattern_results = cfg['mcmc_output']['null_out_pattern']
    else:
        pattern_results = cfg['mcmc_output']['out_pattern']
    pattern_results = pattern_results.strip()
    path_results = pattern_results.format(**cfg) % chrom
    
    archive = tarfile.open(name=path_results, mode='r:*')
    archive.extractall(path=scratch)
    names_npy = archive.getnames()
    archive.close()

    # Load results of interest
    theta   = np.load(scratch + '/theta.npy', mmap_mode='r')
    mu      = np.load(scratch + '/mu.npy', mmap_mode='r')

    # Remove burnin
    if n_burnin > 0:
        mu = mu[n_burnin:]
        theta = theta[n_burnin:]

    # Compute posterior mean of coefficients
    # This looks inefficient, but it saves memory --- a lot of memory
    b_postmean = np.array([np.mean(np.exp(theta_k)) for theta_k in theta.T])
    
    # Setup window for clustering
    cluster_window = gaussian_window(h=h, sigma=cluster_bw)

    # Get cluster centers
    cluster_centers = get_cluster_centers(x=b_postmean, window=cluster_window,
                                          min_spacing=cluster_min_spacing,
                                          edge_correction=True)
    n_clusters = cluster_centers.size

    # Create slices by cluster for efficient access
    cluster_slices = [slice(max(0, c-h), min(c+h+1, theta.shape[1]), 1) for c in
                      cluster_centers]

    # Extract cluster sizes
    cluster_sizes = np.array([s.stop - s.start for s in cluster_slices],
                             dtype=np.int)

    # Create names for sparsity and n_large variables
    names_sparsity = ["sparsityq%02.0f" % (q * 100) for q in q_sparsity]
    names_sparsity_se = ["sparsityq%02.0f_se" % (q * 100) for q in q_sparsity]
    names_nlarge = ["nlargep%02.0f" % (p * 100) for p in p_threshold]
    names_nlarge_se = ["nlargep%02.0f_se" % (p * 100) for p in p_threshold]

    # Allocate arrays for cluster-level summaries
    cluster_summaries = collections.OrderedDict()
    cluster_summaries['center'] = cluster_centers
    cluster_summaries['cluster_length'] = cluster_sizes
    cluster_summaries['occupancy'] = np.empty(n_clusters, dtype=np.float)
    cluster_summaries['occupancy_se'] = np.empty(n_clusters, dtype=np.float) 
    cluster_summaries['localization'] = np.empty(n_clusters, dtype=np.float)
    cluster_summaries['localization_se'] = np.empty(n_clusters, dtype=np.float)
    cluster_summaries['structure'] = np.empty(n_clusters, dtype=np.float)
    cluster_summaries['structure_se'] = np.empty(n_clusters, dtype=np.float)
    cluster_summaries['sparsity'] = np.empty(n_clusters, dtype=np.float)
    cluster_summaries['sparsity_se'] = np.empty(n_clusters, dtype=np.float)
    for var in itertools.chain(
        [item for items in itertools.izip(names_sparsity, names_sparsity_se) for
         item in items],
        [item for items in itertools.izip(names_nlarge, names_nlarge_se) for
         item in items]):
        cluster_summaries[var] = np.empty(n_clusters, dtype=np.float)
    
    # Compute cluster-level summaries, iterating over clusters
    for i, center, cluster in itertools.izip(xrange(n_clusters),
                                             cluster_centers, cluster_slices):
        # Extract cluster coefficient draws
        b_draws = np.exp(theta[:,cluster])
        p_draws = (b_draws.T / np.sum(b_draws, 1)).T
        
        # Compute posterior mean occupancy and its SD
        cluster_summaries['occupancy'][i] = np.mean(b_draws)*cluster_sizes[i]
        cluster_summaries['occupancy_se'][i] = np.std(np.sum(b_draws, axis=1))

        # Compute localization index by draw
        x=np.arange(cluster_sizes[i])[np.newaxis,:]
        localization = localization_index(x=x, p=p_draws, axis=1)
        cluster_summaries['localization'][i] = np.mean(localization)
        cluster_summaries['localization_se'][i] = np.std(localization)

        # Compute structure index by draw
        structure = structure_index(x=b_draws, axis=1)
        cluster_summaries['structure'][i] = np.mean(structure)
        cluster_summaries['structure_se'][i] = np.std(structure)
        
        # Compute sparsity index by draw
        sparsity = sparsity_index(x=b_draws, q=q_sparsity, axis=1)
        for i_q in xrange(len(q_sparsity)):
            cluster_summaries[names_sparsity[i_q]][i] = np.mean(sparsity[i_q])
            cluster_summaries[names_sparsity_se[i_q]][i] = np.std(sparsity[i_q])

        # Compute n_large by draw
        n_large = compute_n_large(x=b_draws, p_threshold=p_threshold, axis=1)
        for i_p in xrange(len(p_threshold)):
            cluster_summaries[names_nlarge[i_p]][i] = np.mean(n_large[i_p])
            cluster_summaries[names_nlarge_se[i_p]][i] = np.std(n_large[i_p])

    # Provide nicely-formatted delimited output for analyses and plotting
    if null:
        pattern_summaries = cfg['mcmc_output']['null_cluster_pattern']
    else:
        pattern_summaries = cfg['mcmc_output']['cluster_pattern']
    pattern_summaries = pattern_summaries.strip()
    path_summaries = pattern_summaries.format(**cfg) % chrom

    # Build recarray of summaries, starting with coefficients and diagnostics
    summaries = np.rec.fromarrays(cluster_summaries.values(),
                                  names=cluster_summaries.keys())

    # Write summaries to delimited text file
    libio.write_recarray_to_file(fname=path_summaries, data=summaries,
                                 header=True, sep=' ')

    # Clean-up scratch directory
    for name in names_npy:
        os.remove(scratch + '/' + name)

    return 0

def summarise_params(cfg, chrom=1, null=False):
    '''
    Coordinate summarisation of MCMC parameter draws.

    Parameters
    ----------
    - cfg : dictionary
        Dictionary of parameters containing at least those relevant MCMC
        draw and summary output paths and parameters for summarization.
    - chrom : int
        Index of chromosome to analyze
    - null : bool
        Summarise null results?

    Returns
    -------
    - status : int
        Integer status for summarisation. 0 for success, > 0 for failure.
    '''
    # Reference useful information in local namespace
    n_burnin    = cfg['mcmc_params']['n_burnin']
    scratch     = cfg['mcmc_summaries']['path_scratch']
    
    # Check for existence and writeability of scratch directory
    if os.access(scratch, os.F_OK):
        # It exists, check for read-write
        if not os.access(scratch, os.R_OK | os.W_OK):
            print >> sys.stderr, ("Error --- Cannot read and write to %s" %
                                  scratch)
            return 1
    else:
        # Otherwise, try to make the directory
        os.makedirs(scratch)

    # Extract results to scratch directory
    if null:
        pattern_results = cfg['mcmc_output']['null_out_pattern']
    else:
        pattern_results = cfg['mcmc_output']['out_pattern']
    pattern_results = pattern_results.strip()
    path_results = pattern_results.format(**cfg) % chrom
    
    archive = tarfile.open(name=path_results, mode='r:*')
    archive.extractall(path=scratch)
    names_npy = archive.getnames()
    archive.close()

    # Load results of interest
    mu = np.load(scratch + '/mu.npy')
    sigmasq = np.load(scratch + '/sigmasq.npy')
    region_ids = np.load(scratch + '/region_ids.npy')

    # Remove burnin
    if n_burnin > 0:
        mu = mu[n_burnin:]
        sigmasq = sigmasq[n_burnin:]

    # Compute posterior means
    mu_postmean = np.mean(mu, 0)
    sigmasq_postmean = np.mean(sigmasq, 0)
    sigma_postmean = np.mean(np.sqrt(sigmasq), 0)

    # Compute posterior medians
    mu_postmed = np.median(mu, 0)
    sigmasq_postmed = np.median(sigmasq, 0)
    sigma_postmed = np.median(np.sqrt(sigmasq), 0)

    # Compute standard errors
    mu_se = np.std(mu, 0)
    sigmasq_se = np.std(sigmasq, 0)
    sigma_se = np.std(np.sqrt(sigmasq), 0)

    # Provide nicely-formatted delimited output for analyses and plotting
    if null:
        pattern_summaries = cfg['mcmc_output']['null_param_pattern']
    else:
        pattern_summaries = cfg['mcmc_output']['param_pattern']
    pattern_summaries = pattern_summaries.strip()
    path_summaries = pattern_summaries.format(**cfg) % chrom

    # Build recarray of summaries, starting with coefficients and diagnostics
    summaries = np.rec.fromarrays([region_ids, mu_postmean, mu_postmed, mu_se,
                                   sigmasq_postmean, sigmasq_postmed,
                                   sigmasq_se, sigma_postmean, sigma_postmed,
                                   sigma_se],
                                  names= ('region_id', 'mu_postmean',
                                          'mu_postmed', 'mu_se',
                                          'sigmasq_postmean', 'sigmasq_postmed',
                                          'sigmasq_se', 'sigma_postmean',
                                          'sigma_postmed', 'sigma_se'))

    # Write summaries to delimited text file
    libio.write_recarray_to_file(fname=path_summaries, data=summaries,
                                 header=True, sep=' ')

    # Clean-up scratch directory
    for name in names_npy:
        os.remove(scratch + '/' + name)

    return 0

def detect_from_summaries(cfg, chrom=1, detect_fmt=("%.1f", "%d")):
    '''
    Detect local concentrations based on existing summaries.

    Parameters
    ----------
    - cfg : dictionary
        Dictionary of parameters containing at least those relevant MCMC
        draw and summary output paths and parameters for summarization.
    - chrom : int
        Index of chromosome to analyze

    Returns
    -------
    - status : int
        Integer status for summarisation. 0 for success, > 0 for failure.
    '''
    # Reference useful information in local namespace
    p_detect = cfg['mcmc_summaries']['p_detect']
    
    # Extract window size information (+/-) from config
    concentration_pm = cfg['mcmc_summaries']['concentration_pm']
    if isinstance(concentration_pm, str):
        pm_list = [int(s) for s in concentration_pm.split(',')]
    else:
        pm_list = [concentration_pm]

    # Get path to posterior summaries
    pattern_summaries = cfg['mcmc_output']['summary_pattern']
    pattern_summaries = pattern_summaries.strip()
    path_summaries = pattern_summaries.format(**cfg) % chrom
    
    # Run detection
    if p_detect is not None:
        # Load summaries
        summaries = np.genfromtxt(path_summaries, names=True)
        
        # Iterate of +/- settings
        for pm in pm_list:
            # Find detected positions
            key = 'p_local_concentration_pm%d' % pm
            detected = np.where(summaries[key] > p_detect)[0]

            # Condense regions
            detected, n = condense_detections(detected)

            # Write detections to text file
            pattern_detections = cfg['mcmc_output']['detections_pattern']
            pattern_detections = pattern_detections.strip()
            path_detections = pattern_detections.format(**cfg) % (chrom, pm)

            detections = np.rec.fromarrays([detected, n],
                                           names=('pos', 'n'))
            libio.write_recarray_to_file(fname=path_detections, data=detections,
                                         header=True, sep=' ', fmt=detect_fmt)

    return 0

