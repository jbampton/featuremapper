"""
This module is used to analyse the hypercolumn structure of preference
maps. Currently this file offers a means to estimate the hypercolumn
distance from the Fourier power spectrum but different types of
analysis (eg. wavelet analysis) may be introduced in future.
"""

__author__ = "Jean-Luc Stevens"

import math
import itertools
import numpy as np
from scipy.optimize import curve_fit

import param

from holoviews import Dimension
from holoviews.core.options import Store, Options
from holoviews import Curve, Histogram, ItemTable, Overlay, Image
from holoviews.element.annotation import VLine
from .raster import fft_power

from .pinwheels import PinwheelAnalysis
from . import TreeOperation

try: # 2.7+
    gamma = math.gamma
except:
    import scipy.special as ss
    gamma = ss.gamma



class PowerSpectrumAnalysis(TreeOperation):
    """
    Estimation of hypercolumn distance in a cyclic preference map from
    the size of the ring in the fourier power spectrum, following the
    methods described in the in the supplementary materials of
    ``Universality in the Evolution of Orientation Columns in the
    Visual Cortex'', Kaschube et al. 2010.

    If supplied with a preference overlayed with pinwheels, the
    pinwheel_density is computed from kmax (the wavenumber of highest
    power) using the equation:

    rho = pinwheel count/(kmax**2)

    This is then used to generate a map quality estimate (with unit
    range) based on the pi-pinwheel density criterion.

    If there is an Image with group 'FFT_Power', then this will be used
    as the polar power spectrum, allowing the analysis of experimental
    maps.
    """


    init_fit = param.Dict(default=None, allow_None=True, doc="""
       If set to None, an initial fit is automatically selected for
       the curve fitting procedure. Otherwise, this is a dictionary of
       the initial coefficients for equation (7) from the 2010 Science
       paper mentioned above (supplementary materials). For instance,
       the values used in the GCAL (Stevens et al. 2013):

       init_fit = dict(a0=0.35, a1=3.8, a2=1.3, a3=0.15, a4=-0.003, a5=0)

       These coefficients may be understood as follows:

        a0 => Gaussian height.
        a1 => Peak x-axis position.
        a2 => Gaussian spread (ie. variance).
        a3 => Baseline value (without falloff).
        a4 => Linear falloff.
        a5 => Quadratic falloff.
       """)

    averaging_fn = param.Callable(default=np.mean, doc="""
      The averaging function used to collapse the power spectrum at each
      wavenumber down to a scalar value. By default, finds the mean
      power for each wavenumber.""")

    fit_table = param.Boolean(default=False, doc="""
      Whether or not to add table listing the fit coefficients at the
      end of the output layout.""")

    gamma_k= param.Number(default=1.8, doc="""
      The degree to which the gamma kernel is heavily tailed when
      squashing the pinwheel density into a unit map metric.""")

    label = param.String(None, allow_None=True, precedence=-1, constant=True,
     doc="""Label suffixes are fixed as there are too many labels to specify.""")


    def _process(self, tree, key=None):

        preference = None
        elements = tree.values()
        for element in tree.values():
            layers = element.values() if isinstance(element, Overlay) else [element]
            for el in layers:
                if isinstance(el, Image) and el.value_dimensions[0].cyclic:
                    preference = el

        if preference is None:
            raise Exception("At least one cyclic matrix required for hypercolumn analysis.")

        pinwheels = self.search(tree, 'Points.Pinwheels')
        if not pinwheels:
            pinwheel_analysis = PinwheelAnalysis(preference)
            elements.pop(elements.index(preference))
            elements.append(pinwheel_analysis) # Don't want to show preference twice
            pinwheels = self.search(pinwheel_analysis, 'Points.Pinwheels')

        pinwheel_count = pinwheels[0].data.shape[0]
        wavenumber_dim = Dimension('Wavenumber', unit='k')

        (l, b, r, t) = preference.bounds.lbrt()
        (dim1, dim2) = preference.data.shape
        xdensity = dim1 / abs(r-l)
        ydensity = dim2 / abs(t-b)

        if xdensity != ydensity:
            raise Exception("Image must have matching x- and y-density")
        self._density = xdensity

        try:
            power_spectrum = self.search(tree, 'Image.FFT_Power')[0]
        except:
            power_spectrum = None
        if not power_spectrum:
            power_spectrum = fft_power(preference)
        (amplitudes, edges), fit, info = self.estimate_hypercolumn_distance(power_spectrum.data)

        kmax = info['kmax']
        info['rho'] = pinwheel_count / (kmax ** 2)
        info['rho_metric'] = self.gamma_metric(info['rho'], gamma_k=self.p.gamma_k)

        if fit is not None:
            samples = self.fit_samples(dim1/2, 100, fit)
        else:
            samples = zip([0, dim1/2], [0.0, 0.0])

        info_table = ItemTable(sorted(info.items()), group='PowerSpectrum Analysis', label=preference.label)
        curve = Curve(samples, key_dimensions=[wavenumber_dim], label=preference.label, group='FFTPowerFit')
        hist = Histogram(amplitudes, edges, key_dimensions=[wavenumber_dim],
                         label=preference.label, group='FFTPowerHistogram')


        vline = VLine(kmax, group='KMax', label=preference.label)
        powerfit = (hist * curve * vline).relabel(group='PowerFit', label=preference.label)
        analysis = [power_spectrum, powerfit, info_table]
        if self.p.fit_table and fit is None:
            fit = dict(('a%i' % i, '-') for i in range(6))

        if self.p.fit_table:
            fit_table = ItemTable(fit, group='CurveFit', label=preference.label)
            analysis.append(fit_table)
        return elements + analysis


    @classmethod
    def gamma_dist(cls, x, k, theta):
        "The gamma distribution used for the gamma metric"
        return (1.0/theta**k)*(1.0/gamma(k)) * x**(k-1) * np.exp(-(x/theta))


    @classmethod
    def gamma_metric(cls, pwd, gamma_k):
        """
        The heavily-tailed gamma kernel used to squash the pinwheel
        density into unit range. The maximum value of unity is
        attained when the input pinwheel density is pi.
        """
        theta = math.pi / (gamma_k -1) # Mode: (k - 1)* theta
        norm = cls.gamma_dist(math.pi, gamma_k, theta)
        return (1.0/norm)*cls.gamma_dist(pwd, gamma_k, theta)


    def wavenumber_spectrum(self, spectrum):
        """
        Bins the power values in the 2D FFT power spectrum as a
        function of wavenumber (1D). Requires square FFT spectra with
        an odd dimension to work to ensure there is a central sample
        corresponding to the DC component (wavenumber zero).
        """
        dim, _dim = spectrum.shape
        assert dim == _dim, "This approach only supports square FFT spectra"
        if not dim % 2:
            self.warning("Slicing data to nearest odd dimensions for centered FFT.")
            spectrum = spectrum[:None if dim % 2 else -1,
                                :None if _dim % 2 else -1]
            dim, _ = spectrum.shape

        # Invert as power_spectrum returns black (low values) for high amplitude
        spectrum = 1 - spectrum
        pixel_bins = range(0, (dim / 2) + 1)
        lower = -(dim / 2)
        upper = (dim / 2) + 1

        # Grid of coordinates relative to central DC component (0,0)
        x, y = np.mgrid[lower:upper, lower:upper]
        flat_pixel_distances = ((x ** 2 + y ** 2) ** 0.5).flatten()
        flat_spectrum = spectrum.flatten()

        # Indices in pixel_bins to which the distances belong
        bin_allocation = np.digitize(flat_pixel_distances, pixel_bins)
        # The bin allocation zipped with actual fft power values
        spectrum_bins = zip(bin_allocation, flat_spectrum)
        grouped_bins = itertools.groupby(sorted(spectrum_bins), lambda x: x[0])
        hist_values = [([sval for (_, sval) in it], bin)
                       for (bin, it) in grouped_bins]
        (power_values, bin_boundaries) = zip(*hist_values)
        averaged_powers = [self.p.averaging_fn(power) for power in power_values]
        assert len(bin_boundaries) == len(pixel_bins)
        return averaged_powers, pixel_bins


    def KaschubeFit(self, k, a0, a1, a2, a3, a4, a5):
        """
        Fitting function used by Kaschube for finding the hypercolumn
        distance from the Fourier power spectrum. These values should
        match the init_fit defaults of pinwheel_analysis below.
        """
        exponent = - ((k - a1)**2) / (2 * a2**2)
        return a0 * np.exp(exponent) + a3 + a4*k + a5*np.power(k,2)


    def fit_samples(self, max_k, samples, fit):
        "Compute a curve based from the fit coefficients"
        ks = np.linspace(0, max_k, max_k)
        values = [self.KaschubeFit(k, **fit) for k in ks]
        return np.array(zip(ks,values))


    def estimate_hypercolumn_distance(self, power_spectrum):
        """
        Estimating the hypercolumn distance by fitting Equation 7 of
        Kaschube et al. 2010 Equation 7 (supplementary
        material). Returns the analysed values as a dictionary.
        """
        amplitudes, edges = self.wavenumber_spectrum(power_spectrum)
        ks = np.array(range(len(amplitudes)))
        try:
            wavenumber_power = amplitudes[:]
            kmax_argmax = float(np.argmax(wavenumber_power[1:]) + 1)
            baseline = np.mean(wavenumber_power)
            height = wavenumber_power[int(kmax_argmax)] - baseline

            if self.p.init_fit is None:
                init_fit = [height, kmax_argmax, 4.0, baseline, 0, 0]
            else:
                init_fit = self.p.init_fit

            fit_vals, _ = curve_fit(self.KaschubeFit,
                                    ks, np.array(amplitudes),
                                    init_fit, maxfev=10000)
            fit = dict(zip(['a0', 'a1', 'a2', 'a3', 'a4', 'a5'], fit_vals))
            valid_fit = (fit['a1'] > 0)
        except:
            valid_fit = False

        kmax_argmax = np.argmax(amplitudes[1:]) + 1
        kmax = fit['a1'] if valid_fit else float(kmax_argmax)

        # The amplitudes begins with k=0 (DC component), k=1 for one
        # period per map, k=2 for two periods per map etc. The units per
        # hypercolumn is the total number of units across the map divided
        # by kmax. If k <= 1.0, the full map width is reported.
        (dim, _) = power_spectrum.shape
        units_per_hypercolumn = dim if (kmax <= 1.0) else dim / float(kmax)
        cycles = self._density / units_per_hypercolumn

        return ((amplitudes, edges),
                fit if valid_fit else None,
                {'kmax': float(kmax),
                'k_delta': float(kmax - float(kmax_argmax)),
                'units_per_hc': float(units_per_hypercolumn),
                'cycles': float(cycles)})


# Defining styles
options = Store.options(backend='matplotlib')
options.Curve.FFTPowerFit = Options('style', color='r', linewidth=3)
options.VLine.KMax = Options('style', color='g', linewidth=3)
options.Histogram.FFTPowerHistogram = Options('style', fc='w', ec='k')
