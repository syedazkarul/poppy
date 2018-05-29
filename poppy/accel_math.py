# accel_math.py
#
# Various functions related to accelerated computations using FFTW, CUDA, numexpr, and related.
#
import numpy as np
import multiprocessing
from . import conf

import logging
_log = logging.getLogger('poppy')



try:
    # try to import FFTW to see if it is available
    import pyfftw
    # Setup infrastructure for FFTW
    _FFTW_INIT = {}  # dict of array sizes for which we have already performed the required FFTW planning step
    _FFTW_FLAGS = ['measure']
    _FFTW_AVAILABLE = True
except ImportError:
    pyfftw = None
    _FFTW_AVAILABLE = False


try:
    # try to import numexpr package to see if it is available
    import numexpr as ne

    _NUMEXPR_AVAILABLE = True
except ImportError:
    ne = None
    _NUMEXPR_AVAILABLE = False

try:
    # try to import anaconda accelerate package to see if it is available
    import pyculib
    from numba import cuda
    _CUDA_PLANS = {} # plans for various array sizes already prepared
    _CUDA_AVAILABLE = True
except ImportError:
    pyculib = None
    _CUDA_AVAILABLE = False

try:
    # try to import pyopencl and gpyfft to see if OpenCL FFT is available
    import pyopencl
    import pyopencl.array
    import gpyfft
    _OPENCL_AVAILABLE = True
    _OPENCL_STATE = dict()
except ImportError:
    _OPENCL_AVAILABLE = False


_USE_CUDA = (conf.use_cuda and _CUDA_AVAILABLE)
_USE_OPENCL = (conf.use_opencl and _OPENCL_AVAILABLE)
_USE_NUMEXPR = (conf.use_numexpr and _NUMEXPR_AVAILABLE)



def _float():
    """ Returns numpy data type for desired precision based on configuration """
    # How many bits per float to use?
    return np.float64 if conf.double_precision else np.float32


def _complex():
    """ Returns numpy data type for desired precision based on configuration """
    # How many bits per complex float to use?
    return np.complex128 if conf.double_precision else np.complex64


def _r(x, y):
    """ Function to speed up computing the radius given x and y, using Numexpr if available
    Otherwise defaults to numpy. """
    if _USE_NUMEXPR:
        return ne.evaluate("sqrt(x**2+y**2)")
    else:
        return np.sqrt(x ** 2 + y ** 2)


def _exp(x):
    """
    Function to speed up taking exponential of an array if NumExpr is available.
    Otherwise defaults to np.exp()

    """
    if _USE_NUMEXPR:
        return ne.evaluate("exp(x)", optimization='moderate', )
    else:
        return np.exp(x)

def _fftshift(x):
    """ FFT shifts of array contents, using CUDA if available.
    Otherwise defaults to numpy.

    Note - TODO write an OpenCL version

    See also ifftshift
    """

    N=x.shape[0]
    # the CUDA fftshift is set up to work on blocks of 32, so
    # N must be a multiple of 32. We check this rapidly using a bit mask:
    #    (x & 31)==0  is a ~20x faster equivalent of (np.mod(x,32)==0)
    if (_USE_CUDA) & (N==x.shape[1]) & ((N & 31)==0):
        blockdim = (32, 32) # threads per block
        numBlocks = (int(N/blockdim[0]),int(N/blockdim[1]))
        cufftShift_2D_kernel[numBlocks, blockdim](x.ravel(),N)
        return x
    else:
        return np.fft.fftshift(x)

def _ifftshift(x):
    """ Inverse FFT shifts of array contents, using CUDA if available.
    Otherwise defaults to numpy.
    Note, ifftshift and fftshift are identical for even-length x,
    the functions differ by one sample for odd-length x. This function
    implictly assumes that if using CUDA, the array size must be even,
    so we can use the same algorithm as fftshift.

    Note - TODO write an OpenCL version

    See also fftshift
    """

    N=x.shape[0]
    # the CUDA fftshift is set up to work on blocks of 32, so
    # N must be a multiple of 32. We check this rapidly using a bit mask:
    #   not (x & 31)  is a ~20x faster equivalent of (np.mod(x,32)==0)
    if (_USE_CUDA) & (N==x.shape[1]) & ((N & 31)==0):
        blockdim = (32, 32) # threads per block
        numBlocks = (int(N/blockdim[0]),int(N/blockdim[1]))
        cufftShift_2D_kernel[numBlocks, blockdim](x.ravel(),N)
        return x
    else:
        return np.fft.ifftshift(x)




def fft_2d(wavefront, forward=True, normalization=None, fftshift=True):
    """ main entry point for FFTs, used in Wavefront._propagate_fft and
    elsewhere. This will invoke one of the following, depending on availability:
        - CUDA on NVidia GPU
        - OpenCL on AMD GPU
        - FFTW on CPU
        - numpy on CPU

    This function handles ONLY the core numerics itself, as fast as possible,
    (and some minor related logging) .
    All the interaction with object state for Wavefront arrays should happen elsewhere.

    TODO: this should execute an IN PLACE FFT, so we don't have to pass around arrays to return
    anything.

    Parameters
    -----------
    forward : bool
        set to True for forward FFT, False for inverse fft
    normalization : float, optional
        Normalization factor. Defaults to 1./wavefront.shape[0] for forward,
        and wavefront.shape[0] for inverse. Use this only if you need a non-default
        behavior.
    fftshift : bool
        apply FFT shift after forwards FFT or before inverse FFT?

    """
    # To use a fast FFT, it must both be enabled and the library itself has to be present
    _USE_CUDA = (conf.use_cuda and _CUDA_AVAILABLE)
    _USE_OPENCL = (conf.use_opencl and _OPENCL_AVAILABLE)
    _USE_FFTW = (conf.use_fftw and _FFTW_AVAILABLE)

    # OpenCL cfFFT only can FFT certain array sizes.
    # This check is more stringent that necessary - opencl can handle powers of a few small integers
    # but this simple version helps during development
    if _USE_OPENCL and not ispowerof2(wavefront.shape[0]):
        _log.debug(("Wavefront size {} not supported by OpenCL, therefore disabling "+
            "USE_OPENCL for this calculation.").format(wavefront.shape))
        _USE_OPENCL = False

    # This annoyingly complicated if/elif is just for the debug print statement
    if _USE_CUDA:
        method = 'pyculib (CUDA GPU)'
    elif _USE_OPENCL:
        method = 'pyopencl (OpenCL GPU)'
    elif _USE_FFTW:
        method = 'pyfftw'
    else:
        method = 'numpy'
    _log.debug("using {2} FFT of {0} array, FFT_direction={1}".format(str(wavefront.shape), 'forward' if forward else 'backward', method))

    if (not forward) and fftshift: #inverse shift before backwards FFTs
        wavefront = _ifftshift(wavefront)

    if _USE_CUDA:
        if normalization is None:
            normalization = 1./wavefront.shape[0]  # regardless of direction, for CUDA

        # We need a CUDA FFT plan for each size and shape of FFT.
        # The plans can be cached for reuse, since they cost some 
        # 10s of milliseconds to create
        params = (wavefront.shape, wavefront.dtype, wavefront.dtype)
        try:
            cufftplan = _CUDA_PLANS[params]
        except KeyError:
            cufftplan = pyculib.fft.FFTPlan(*params)
            _CUDA_PLANS[params] = cufftplan

        # perform FFT on GPU, and return results in place to same array.
        if forward:
            cufftplan.forward(wavefront, out=wavefront)
        else:
            cufftplan.inverse(wavefront, out=wavefront)

    elif _USE_OPENCL:
        if normalization is None:
            normalization = 1./wavefront.shape[0] if forward else wavefront.shape[0]

        context, queue = get_opencl_context()
        wf_on_gpu = pyopencl.array.to_device(queue, wavefront)
        transform = gpyfft.fft.FFT(context, queue, wf_on_gpu, axes=(0,1))
        event, = transform.enqueue(forward=forward)
        event.wait()
        wavefront[:] = wf_on_gpu.get()
        del wf_on_gpu

    elif _USE_FFTW:
        FFT_direction = 'forward' if forward else 'backward' # back compatible for use in _FFTW_INIT
        do_fft = pyfftw.interfaces.numpy_fft.fft2 if forward else pyfftw.interfaces.numpy_fft.ifft2
        if normalization is None:
            normalization = 1./wavefront.shape[0] if forward else wavefront.shape[0]


        if (wavefront.shape, FFT_direction) not in _FFTW_INIT:
            # The first time you run FFTW to transform a given size, it does a speed test to
            # determine optimal algorithm that is destructive to your chosen array.
            # So only do that test on a copy, not the real array:
            _log.info("Measuring pyfftw optimal plan for %s, direction=%s" % (
                str(wavefront.shape), FFT_direction))

            pyfftw.interfaces.cache.enable()
            pyfftw.interfaces.cache.set_keepalive_time(30)

            test_array = np.zeros(wavefront.shape)
            test_array = do_fft(test_array, overwrite_input=True, planner_effort='FFTW_MEASURE',
                                threads=multiprocessing.cpu_count())

            _FFTW_INIT[(wavefront.shape, FFT_direction)] = True

        wavefront = do_fft(wavefront, overwrite_input=True, planner_effort='FFTW_MEASURE',
                                threads=multiprocessing.cpu_count())
    else: # Basic numpy FFT
        do_fft =  np.fft.fft2 if forward else np.fft.ifft2
        if normalization is None:
            normalization = 1./wavefront.shape[0] if forward else wavefront.shape[0]
        wavefront = do_fft(wavefront)

    if forward and fftshift:
        wavefront = _fftshift(wavefront)

    wavefront *= normalization

    return wavefront



def ispowerof2(num):
    """ Is this number a power of 2?"""
    # see http://code.activestate.com/recipes/577514-chek-if-a-number-is-a-power-of-two/
    return (num & (num-1) == 0)

if _OPENCL_AVAILABLE:
    def get_opencl_context():
        """ Create, save, and retrieve OpenCL handles to the GPU """
        if len(_OPENCL_STATE) == 0:
            platforms = pyopencl.get_platforms()
            if len(platforms) == 1:
                _OPENCL_STATE['platform'] = platforms[0]
            else:
                raise RuntimeError("OpenCL code needs update for multiple platforms")
            gpus = _OPENCL_STATE['platform'].get_devices(device_type=pyopencl.device_type.GPU)
            if len(gpus) == 1:
                device = gpus[0]
                _OPENCL_STATE['device'] = device
            else:
                #raise RuntimeError("OpenCL code could not uniquely identify which device to use as GPU")
                device = gpus[1]
                _log.warning('Caution - hard coded use of gpu #1 if > 1 GPUs present')
            context = pyopencl.Context(devices=[device])
            queue = pyopencl.CommandQueue(context)

            _OPENCL_STATE['context'] = context
            _OPENCL_STATE['queue'] = queue
        return (_OPENCL_STATE['context'], _OPENCL_STATE['queue'])





if  _USE_CUDA:
    @cuda.jit()
    def cufftShift_2D_kernel(data, N):
        """
        adopted CUDA FFT shift code from:
        https://github.com/marwan-abdellah/cufftShift
        (GNU Lesser Public License)
        """

        # // 2D Slice & 1D Line
        sLine = N
        sSlice = N * N
        # // Transformations Equations
        sEq1 = int((sSlice + sLine) / 2)
        sEq2 = int((sSlice - sLine) / 2)
        x, y = cuda.grid(2)
        # // Thread Index Converted into 1D Index
        index = (y * N) + x

        if x < N / 2:
            if y < N / 2:
                # // First Quad
                temp = data[index]
                data[index] = data[index + sEq1]
                # // Third Quad
                data[index + sEq1] = temp
        else:
            if y < N / 2:
                # // Second Quad
                temp = data[index]
                data[index] = data[index + sEq2]
                data[index + sEq2] = temp
