import netCDF4
import numpy as np
import sys
import xarray as xr
from .brdf_model_M02 import M02
from .brdf_model_M02SeaDAS import M02SeaDAS
from .brdf_model_L11 import L11
from .brdf_model_O23 import O23
from .brdf_utils import ADF_OCP

"""
Main BRDF correction module
    Works with xarray dataset as input
    Required spectral dimension is "bands", others dimensions are free
    Required fields in input dataset:
        Rw: directional marine reflectance
        sza: sun zenith angle
        vza: view zenith angle
        raa: relative azimuth angle (raa=0 for sun and view on same side)
    Optional fields in inout dataset:
        Rw_unc: uncertainty of Rw (if missing, set to zero)
    Output fields in dataset: 
        nrrs: fully normalized remote-sensing reflectance
        rho_ex_w: nrrs * PI
        omega_b: bb/(a+bb)
        eta_b: bbw/bb
        C_brdf: BRDF correction factor
        brdf_unc: uncertainty of C_brdf
        nrrs_unc : uncertainty of nrrs

    Information to keep for any usage and adaptation of the brdf_hypercp module:
        Brdf_hypercp is part of the EUMETSAT study 
        "BRDF correction of S3 OLCI water reflectance products", 
        Contract N.: RB_EUM-CO-21-4600002626-JIG. 
        Study team members: Davide  D'Alimonte (davide.dalimonte@aequora.org), 
        Tamito Kajiyama (tamito.kajiyama@aequora.org), 
        Jaime Pitarch (jaime.pitarchportero@artov.ismar.cnr.it), 
        Vittorio Brando (vittorio.brando@cnr.it),  
        Marco Talone (talone@icm.csic.es) and 
        Constant Mazeran (constant.mazeran@solvo.fr).
    Relative azimuth in the BRDF LUTs follows the OLCI convention. See https://www.eumetsat.int/media/50720, Fig. 6.
"""


def brdf_prototype(ds, adf=None, brdf_model='L11'):
    # TEST brdf_models not supported in the GUI: hard overwrite
    # brdf_model = 'M02SeaDAS'

    # Initialise model
    if brdf_model == 'M02':
        BRDF_model = M02(bands=ds.bands, aot=ds.aot, wind=ds.wind, adf=None)  # Don't use brdf_py.ADF context
    elif brdf_model == 'M02SeaDAS':
        BRDF_model = M02SeaDAS(bands=ds.bands, adf=None)  # Don't use brdf_py.ADF context
    elif brdf_model == 'L11':
        BRDF_model = L11(bands=ds.bands, adf=None)  # Don't use brdf_py.ADF context
    elif brdf_model == 'O23':
        BRDF_model = O23(bands=ds.bands, adf=None)  # Don't use brdf_py.ADF context
    else:
        print("BRDF model %s not supported" % brdf_model)
        sys.exit(1)

    # Init pixel
    BRDF_model.init_pixels(ds['sza'], ds['vza'], ds['raa'])

    # Compute IOP and normalize by iterating
    ds['nrrs'] = ds['Rw'] / np.pi

    ds['convergeFlag'] = (0 * ds['sza']).astype(bool)
    ds['C_brdf'] = 0 * ds['nrrs'] + 1

    for iter_brdf in range(int(BRDF_model.niter)):

        ds = BRDF_model.backward(ds, iter_brdf)

        if brdf_model in ['M02', 'M02SeaDAS']:
            # Initialise chl_iter
            if iter_brdf == 0:
                chl_iter = {}
                chl_iter[-1] = 0 * ds['sza'] + float(BRDF_model.OC4MEchl0)

            chl_iter[iter_brdf] = 10 ** ds['log10_chl']
            #  Check if convergence is reached |chl_old-chl_new| < epsilon * chl_new
            ds['convergeFlag'] = (ds['convergeFlag']) | (
                (np.abs(chl_iter[iter_brdf - 1] - chl_iter[iter_brdf]) < float(BRDF_model.OC4MEepsilon) * chl_iter[
                    iter_brdf]))

        # Apply forward model in both geometries
        forward_mod = BRDF_model.forward(ds).transpose('n', 'bands')
        forward_mod0 = BRDF_model.forward(ds, normalized=True).transpose('n', 'bands')

        # Normalize reflectance
        ds['C_brdf'] = xr.where(ds['convergeFlag'], ds['C_brdf'], forward_mod0 / forward_mod)
        ds['nrrs'] = ds['Rw'] / np.pi * ds['C_brdf']

    # Flag BRDF where NaN and set to 1 (no correction applied).
    ds['C_brdf_fail'] = np.isnan(ds['C_brdf'])
    ds['C_brdf'] = xr.where(ds['C_brdf_fail'], 1, ds['C_brdf'])

    # If QAA_fail is raised, raise C_brdf_fail (but still apply C_brdf).
    if 'QAA_fail' in ds:
        ds['C_brdf_fail'] = (ds['C_brdf_fail']) | (ds['QAA_fail'])

    # Compute uncertainty
    brdf_uncertainty(ds)

    # Compute flag
    ds['flags_level2'] = ds['Rw'] * 0  # TODO

    # Convert to reflectance unit
    ds['rho_ex_w'] = ds['nrrs'] * np.pi

    return ds


''' Compute uncertainty of BRDF factor and propagate to nrrs '''


def brdf_uncertainty(ds, adf=None):
    # Read LUT
    if adf is None:
        adf = ADF_OCP
    # LUT = xr.open_dataset(adf,group='BRDF').unc
    LUT = xr.open_dataset(adf % 'UNC', engine='netcdf4')

    # Interpolate relative uncertainty
    unc = LUT['unc'].interp(lambda_unc=ds.bands, theta_s_unc=ds.theta_s, theta_v_unc=ds.theta_v,
                            delta_phi_unc=ds.delta_phi)

    # Compute absolute uncertainty of factor
    ds['brdf_unc'] = unc * ds['C_brdf']

    # Flag BRDF_unc where NaN and set to 0
    ds['brdf_unc_fail'] = np.isnan(ds['brdf_unc'])
    ds['brdf_unc'] = xr.where(ds['brdf_unc_fail'], 0, ds['brdf_unc'])

    # Propagate to nrrs
    nrrs_unc2 = ds['brdf_unc'] * ds['brdf_unc'] * ds['Rw'] * ds['Rw']
    if 'Rw_unc' in ds:
        nrrs_unc2 += ds['C_brdf'] * ds['C_brdf'] * ds['Rw_unc'] * ds['Rw_unc']
    ds['nrrs_unc'] = np.sqrt(nrrs_unc2)



