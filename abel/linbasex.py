# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import numpy as np
import abel
import os
from glob import glob
import scipy
from scipy.special import eval_legendre
from scipy import ndimage

###############################################################################
# linbasex - inversion procedure based on 1-dimensional projections of
#            velocity-map images
#
# As described in:
#   Gerber, Thomas, Yuzhu Liu, Gregor Knopp, Patrick Hemberger, Andras Bodi,
#   Peter Radi, and Yaroslav Sych,
#     “Charged Particle Velocity Map Image Reconstruction with One-Dimensional
#      Projections of Spherical Functions.”
#     Review of Scientific Instruments 84, no. 3 (March 1, 2013):
#                                      033101–033101 – 10.
#     doi:10.1063/1.4793404.
#
# 2016-04- Thomas Gerber and Daniel Hickstein - theory and code updates
# 2016-04- Stephen Gibson core code extracted from the supplied jupyter
#          notebook (see #167: https://github.com/PyAbel/PyAbel/issues/167)
#
###############################################################################

_linbasex_parameter_docstring = \
    r"""Inverse Abel transform using 1d projections of images.

    Th. Gerber, Yu. Liu, G. Knopp, P. Hemberger, A. Bodi, P. Radi, Ya. Sych,
    "Charged particle velocity map image reconstruction with one-dimensional
    projections of spherical functions",
    `Rev. Sci. Instrum. 84, 033101 (2013)
    <https://doi.org/10.1063/1.4793404>`__.

    ``linbasex`` models the image using a sum of Legendre polynomials at each
    radial pixel, As such, it should only be applied to situations that can
    be adequately represented by Legendre polynomials, i.e., images that
    feature spherical-like structures.  The reconstructed 3D object is
    obtained by adding all the contributions, from which slices are derived.


    Parameters
    ----------
    IM : numpy 2D array
        image data must be square shape of odd size
    proj_angles : list
        projection angles, in radians (default :math:`[0, \pi/2]`)
        e.g. :math:`[0, \pi/2]` or :math:`[0, 0.955, \pi/2]` or :math:`[0, \pi/4, \pi/2, 3\pi/4]`
    legendre_orders : list
        orders of Legendre polynomials to be used as the expansion

        * even polynomials [0, 2, ...] gerade
        * odd polynomials [1, 3, ...] ungerade
        * all orders [0, 1, 2, ...].

        In a single photon experiment there are only anisotropies up to
        second order. The interaction of 4 photons (four wave mixing) yields
        anisotropies up to order 8.
    radial_step : int
        number of pixels per Newton sphere (default 1)
    smoothing: float
        convolve Beta array with a Gaussian function of 1/e 1/2 width `smoothing`.
    rcond : float
        (default 0.0005) scipy.linalg.lstsq fit conditioning value.
        set rcond to zero to switch conditioning off.
        Note: In the presence of noise the equation system may be ill posed.
        Increasing rcond smoothes the result, lowering it beyond a minimum
        renders the solution unstable. Tweak rcond to get a "reasonable"
        solution with acceptable resolution.
    clip : int
        clip first vectors (smallest Newton spheres) to avoid singularities
        (default 0)
    norm_range : tuple
        (low, high)
        normalization of Newton spheres, maximum in range Beta[0, low:high].
        Note: Beta[0, i] the total number of counts integrated over sphere i,
        becomes 1.
    threshold : float
        threshold for normalization of higher order Newton spheres (default 0.2)
        Set all Beta[j], j>=1 to zero if the associated Beta[0] is smaller
        than threshold.
    return_Beta : bool
        return the Beta array of Newton spheres, as the tuple: radial-grid, Beta
        for the case :attr:`legendre_orders=[0, 2]`

            Beta[0] vs radius -> speed distribution

            Beta[2] vs radius -> anisotropy of each Newton sphere

        see 'Returns'.
    direction : str
        "inverse" - only option for this method.
        Abel transform direction.
    dr : None
        dummy variable for call compatibility with the other methods
    verbose : bool
        print information about processing (normally used for debugging)


    Returns
    -------
    inv_IM : numpy 2D array
       inverse Abel transformed image

    radial, Beta, projections : tuple
       (if :attr:`return_Beta=True`)

       contributions of each spherical harmonic :math:`Y_{i0}` to the 3D
       distribution contain all the information one can get from an experiment.
       For the case :attr:`legendre_orders=[0, 2]`:

           Beta[0] vs radius -> speed distribution

           Beta[1] vs radius -> anisotropy of each Newton sphere.

       projections : are the radial projection profiles at angles `proj_angles`

    """

# cache basis
_basis = None
_los = None   # legendre_orders string
_pas = None   # proj_angles string
_radial_step = None
_clip = None


def linbasex_transform(IM, basis_dir=None, proj_angles=[0, np.pi/2],
                       legendre_orders=[0, 2], radial_step=1, smoothing=0,
                       rcond=0.0005, threshold=0.2, return_Beta=False, clip=0,
                       norm_range=(0, -1), direction="inverse", verbose=False,
                       dr=None):
    """
    Wrapper function for linbasex to process supplied quadrant-image as a
    full-image.

    PyAbel transform functions operate on the right side of an image.
    Here we follow the `basex` technique of duplicating the right side to
    the left re-forming the whole image.

    """
    IM = np.atleast_2d(IM)

    quad_rows, quad_cols = IM.shape
    full_image = abel.tools.symmetry.put_image_quadrants((IM, IM, IM, IM),
                 original_image_shape=(quad_rows*2-1, quad_cols*2-1))

    # inverse Abel transform
    recon, radial, Beta, QLz = linbasex_transform_full(full_image,
                               basis_dir=basis_dir, proj_angles=proj_angles,
                               legendre_orders=legendre_orders,
                               radial_step=radial_step, smoothing=smoothing,
                               threshold=threshold, clip=clip,
                               norm_range=norm_range,
                               verbose=verbose)

    # unpack right-side
    inv_IM = abel.tools.symmetry.get_image_quadrants(recon)[0]

    if return_Beta:
        return inv_IM, radial, Beta, QLz
    else:
        return inv_IM


def linbasex_transform_full(IM, basis_dir=None, proj_angles=[0, np.pi/2],
                            legendre_orders=[0, 2],
                            radial_step=1, smoothing=0,
                            rcond=0.0005, threshold=0.2, clip=0,
                            return_Beta=False, norm_range=(0, -1),
                            direction="inverse", verbose=False):
    """interface function that fetches/calculates the Basis and
       then evaluates the linbasex inverse Abel transform for the image.

    """

    IM = np.atleast_2d(IM)

    rows, cols = IM.shape

    if cols % 2 == 0:
        raise ValueError('image width ({}) must be odd and equal to the height'
                         .format(cols))

    if rows != cols:
        raise ValueError('image has shape ({}, {}), '.format(rows, cols) +
                         'must be square for a "linbasex" transform')

    # generate basis or read from file if available
    _basis = get_bs_cached(cols, basis_dir=basis_dir, proj_angles=proj_angles,
                  legendre_orders=legendre_orders, radial_step=radial_step,
                  clip=clip, verbose=verbose)

    return _linbasex_transform_with_basis(IM, _basis, proj_angles=proj_angles,
                                    legendre_orders=legendre_orders,
                                    radial_step=radial_step,
                                    rcond=rcond, smoothing=smoothing,
                                    threshold=threshold, clip=clip,
                                    norm_range=norm_range)


def _linbasex_transform_with_basis(IM, Basis, proj_angles=[0, np.pi/2],
                                   legendre_orders=[0, 2], radial_step=1,
                                   rcond=0.0005, smoothing=0, threshold=0.2,
                                   clip=0, norm_range=(0, -1)):
    """linbasex inverse Abel transform evaluated with supplied basis set Basis.

    """

    IM = np.atleast_2d(IM)

    rows, cols = IM.shape

    # Number of used polynoms
    pol = len(legendre_orders)

    # How many projections
    proj = len(proj_angles)

    QLz = np.zeros((proj, cols))  # array for projections.

    # Rotate and project VMI-image for each angle (as many as projections)
    # if proj_angles == [0, np.pi/2]:
    #     # If coordinates of the detector coincide with the projection
    #     # directions unnecessary rotations are avoided
    #     # i.e. proj_angles=[0, np.pi/2] degrees
    #     QLz[0] = np.sum(IM, axis=1)
    #     QLz[1] = np.sum(IM, axis=0)
    # else:
    for i in range(proj):
        Rot_IM = scipy.ndimage.interpolation.rotate(IM,
                       proj_angles[i]*180/np.pi, axes=(1, 0), reshape=False)
        QLz[i, :] = np.sum(Rot_IM, axis=1)

    # arrange all projections for input into "lstsq"
    bb = np.concatenate(QLz, axis=0)

    Beta = _beta_solve(Basis, bb, pol, rcond=rcond)

    inv_IM, Beta_convol = _Slices(Beta, legendre_orders, smoothing=smoothing)

    # normalize
    Beta = _single_Beta_norm(Beta_convol, threshold=threshold,
                             norm_range=norm_range)

    radial = np.linspace(clip, cols//2, len(Beta[0]))

    # Fix Me! Issue #202 the correct scaling factor for inv_IM intensity?
    return inv_IM, radial, Beta, QLz


linbasex_transform_full.__doc__ = _linbasex_parameter_docstring


def _beta_solve(Basis, bb, pol, rcond=0.0005):
    # set rcond to zero to switch conditioning off

    # array for solutions. len(Basis[0,:])//pol is an integer.
    Beta = np.zeros((pol, len(Basis[0])//pol))

    # solve equation
    Sol = np.linalg.lstsq(Basis, bb, rcond)

    # arrange solutions into subarrays for each β.
    Beta = Sol[0].reshape((pol, len(Sol[0])//pol))

    return Beta


def _SL(i, x, y, Beta_convol, index, legendre_orders):
    """Calculates interpolated β(r), where r= radius"""
    r = np.sqrt(x**2 + y**2 + 0.1)  # + 0.1 to avoid divison by zero.

    # normalize: divison by circumference.
    # @stggh 2/r to correctly normalize intensity cf O2 PES
    BB = np.interp(r, index, Beta_convol[i, :], left=0)/(4*np.pi*r*r)

    return BB*eval_legendre(legendre_orders[i], x/r)


def _Slices(Beta, legendre_orders, smoothing=0):
    """Convolve Beta with a Gaussian function of 1/e width smoothing.

    """

    pol = len(legendre_orders)
    NP = len(Beta[0])  # number of points in 3_d plot.
    index = range(NP)

    Beta_convol = np.zeros((pol, NP))
    Slice_3D = np.zeros((pol, 2*NP, 2*NP))

    # Convolve Beta's with smoothing function
    if smoothing > 0:
        # smoothing function
        Basis_s = np.fromfunction(lambda i: np.exp(-(i - (NP)/2)**2 /
                                  (2*smoothing**2))/(smoothing*2.5), (NP,))
        for i in range(pol):
            Beta_convol[i] = np.convolve(Basis_s, Beta[i], mode='same')
    else:
        Beta_convol = Beta

    # Calculate ordered slices:
    for i in range(pol):
        Slice_3D[i] = np.fromfunction(lambda k, l: _SL(i, (k-NP), (l-NP),
                       Beta_convol, index, legendre_orders), (2*NP, 2*NP))
    # Sum ordered slices up
    Slice = np.sum(Slice_3D, axis=0)

    return Slice, Beta_convol


def int_beta(Beta, radial_step=1, threshold=0.1, regions=None):
    """Integrate beta over a range of Newton spheres.

    Parameters
    ----------
    Beta : numpy array
        Newton spheres
    radial_step : int
        number of pixels per Newton sphere (default 1)
    threshold : float
        threshold for normalisation of higher orders, 0.0 ... 1.0.
    regions : list of tuple radial ranges
        [(min0, max0), (min1, max1), ...]

    Returns
    -------
    Beta_in : numpy array
        integrated normalized Beta array [Newton sphere, region]

    """
    pol = Beta.shape[0]
    # Define new array for normalized beta's, independent of Beat_norm
    Beta_n = np.zeros(Beta.shape)

    # Normalized to Newton sphere with maximal counts.
    max_counts = max(Beta[0, :])

    Beta_n[0] = Beta[0]/max_counts
    for i in range(1, pol):
        Beta_n[i] = np.where(Beta[0]/max_counts > threshold, Beta[i]/Beta[0],
                             0)

    Beta_int = np.zeros((pol, len(regions)))   # arrays for results

    for j, reg in enumerate(regions):
        for i in range(pol):
            Beta_int[i, j] = np.sum(Beta_n[i, range(*reg)])/(reg[1]-reg[0])

    return Beta_int


def _single_Beta_norm(Beta, threshold=0.2, norm_range=(0, -1)):
    """Normalize Newton spheres.

    Parameters
    ----------
    Beta : numpy array
        Newton spheres
    threshold : float
        choose only Beta's for which Beta0 is greater than the maximal Beta0
        times threshold in the chosen range
        Set all βi, i>=1 to zero if the associated β0 is smaller than threshold

    norm_range : tuple (int, int)
        (low, high)
        normalize to Newton sphere with maximum counts in chosen range.
        Beta[0, low:high]

    Return
    ------
    Beta : numpy array
        normalized Beta array

    """
    pol = Beta.shape[0]

    Beta_norm = np.zeros_like(Beta)
    # Normalized to Newton sphere with maximum counts in chosen range.
    max_counts = Beta[0, norm_range[0]:norm_range[1]].max()

    Beta_norm[0] = Beta[0]/max_counts
    for i in range(1, pol):
        Beta_norm[i] = np.where(Beta[0]/max_counts > threshold,
                                Beta[i]/Beta[0], 0)

    return Beta_norm


def _bas(ord, angle, COS, TRI):
    """Define Basis vectors for a given polynomial order "order" and a
       given projection angle "angle".

    """

    basis_vec = scipy.special.eval_legendre(ord, angle) *\
                scipy.special.eval_legendre(ord, COS) * TRI
    return basis_vec


def _bs_linbasex(cols, proj_angles=[0, np.pi/2], legendre_orders=[0, 2],
                 radial_step=1, clip=0):

    pol = len(legendre_orders)
    proj = len(proj_angles)

    # Calculation of Base vectors
    # Define triangular matrix containing columns :math:`x/y` (representing :math:`\cos(\theta))`.
    n = cols//2 + cols % 2
    Index = np.indices((n, n))
    Index[:, 0, 0] = 1
    cos = Index[0]*np.tri(n, n, k=0)[::-1, ::-1]/np.diag(Index[0])

    # Concatenate to "bi"-triangular matrix
    COS = np.concatenate((-cos[::-1, :], cos[1:, :]), axis=0)
    TRI = np.concatenate((np.tri(n, n, k=0,)[:-1, ::-1],
                          np.tri(n, n, k=0,)[::-1, ::-1]), axis=0)

    # radial_step: use only each radial_step other vector.
    # Keep the base vector with full span
    COS = COS[:, ::-radial_step]
    TRI = TRI[:, ::-radial_step]

    COS = COS[:, ::-1]  # rearrange base vectors again in ascending order
    TRI = TRI[:, ::-1]

    if clip > 0:
        # clip first vectors (smallest Newton spheres) to avoid singularities
        COS = COS[:, clip:]
        # It is difficult to trace the effect on the SVD solver used below.
        TRI = TRI[:, clip:]  # usually no clipping works fine.

    # Calculate base vectors for each projection and each order.
    B = np.zeros((pol, proj, len(COS[:, 0]), len(COS[0, :])))

    Norm = np.sum(_bas(0, 1, COS, TRI), axis=0)  # calculate normalization
    cos_an = np.cos(proj_angles)  # angles in radians

    for p in range(pol):
        for u in range(proj):
            B[p, u] = _bas(legendre_orders[p], cos_an[u], COS, TRI)/Norm

    # concatenate vectors to one matrix of bases
    Bpol = np.concatenate((B), axis=2)
    Basis = np.concatenate((Bpol), axis=0)

    return Basis


def get_bs_cached(cols, basis_dir=None, legendre_orders=[0, 2],
                  proj_angles=[0, np.pi/2],
                  radial_step=1, clip=0, verbose=False):
    """load basis set from disk, generate and store if not available.

    Checks whether file:
    ``linbasex_basis_{cols}_{legendre_orders}_{proj_angles}_{radial_step}_{clip}*.npy`` is present in `basis_dir`

    Either, read basis array or generate basis, saving it to the file.


    Parameters
    ----------
    cols : int
        width of image

    basis_dir : str or None
        path to the directory for saving / loading the basis. Use ``''`` for
        the default directory. If ``None``, the basis set will not be loaded
        from or saved to disk.

    legendre_orders : list
        default [0, 2] = 0 order and 2nd order polynomials

    proj_angles : list
        default [0, np.pi/2] in radians

    radial_step : int
        pixel grid size, default 1

    clip : int
        image edge clipping, default 0 pixels

    verbose: boolean
        print information for debugging

    Returns
    -------
    D : tuple (B, Bpol)
       of ndarrays B (pol, proj, cols, cols) Bpol (pol, proj)

    file.npy: file
       saves basis to file name ``linbasex_basis_{cols}_{legendre_orders}_{proj_angles}_{radial_step}_{clip}.npy``

    """

    # cached basis
    global _basis, _los, _pas, _radial_step, _clip

    # legendre_orders string
    los = ''.join(map(str, legendre_orders))
    # convert to % of pi
    proj_angles_fractpi = np.array(proj_angles)*100/np.pi
    # projection angles string
    pas = ''.join(map(str, proj_angles_fractpi.astype(int)))

    if _basis is not None:
        # check basis array sizes, warning may not be unique
        if _basis.shape == (2*cols, cols+1):
            if _los == los and _pas == pas and _radial_step == radial_step and\
               _clip == clip:
                if verbose:
                    print('Using memory cached basis')
                return _basis

    # Fix Me! not a simple unique naming mechanism
    basis_name = "linbasex_basis_{}_{}_{}_{}_{}.npy".format(cols, los, pas,
                                                            radial_step, clip)

    _los = los
    _pas = pas
    _radial_step = radial_step
    _clip = clip
    if basis_dir == '':
        basis_dir = abel.transform.get_basis_dir(make=True)
    if basis_dir is not None:
        path_to_basis_file = os.path.join(basis_dir, basis_name)
        if os.path.exists(path_to_basis_file):
            if verbose:
                print('loading {} ...'.format(path_to_basis_file))
            _basis = np.load(path_to_basis_file)
            return _basis

    if verbose:
        print("A suitable basis for linbasex was not found.\n"
              "A new basis will be generated.")

    _basis = _bs_linbasex(cols, proj_angles=proj_angles,
                     legendre_orders=legendre_orders, radial_step=radial_step,
                     clip=clip)

    if basis_dir is not None:
        path_to_basis_file = os.path.join(basis_dir, basis_name)
        np.save(path_to_basis_file, _basis)
        if verbose:
            print("linbasex basis saved for later use to {}"
                  .format(path_to_basis_file))

    return _basis


def cache_cleanup():
    """
    Utility function.

    Frees the memory caches created by ``get_bs_cached()``.
    This is usually pointless, but might be required after working
    with very large images, if more RAM is needed for further tasks.

    Parameters
    ----------
    None

    Returns
    -------
    None
    """

    global _basis, _los, _pas, _radial_step, _clip

    _basis = None
    _los = None
    _pas = None
    _radial_step = None
    _clip = None


def basis_dir_cleanup(basis_dir=''):
    """
    Utility function.

    Deletes basis sets saved on disk.

    Parameters
    ----------
    basis_dir : str or None
        relative or absolute path to the directory with saved basis sets. Use
        ``''`` for the default directory. ``None`` does nothing.

    Returns
    -------
    None
    """
    if basis_dir == '':
        basis_dir = abel.transform.get_basis_dir(make=False)

    if basis_dir is None:
        return

    files = glob(os.path.join(basis_dir, 'linbasex_basis_*.npy'))
    for fname in files:
        os.remove(fname)


linbasex_transform.__doc__ += _linbasex_parameter_docstring
