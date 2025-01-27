#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © 2022 Michael J. Hayford
""" Vignetting and clear aperture setting operations

.. Created on Mon Apr 18 15:28:25 2022

.. codeauthor: Michael J. Hayford
"""
import logging

import numpy as np
from numpy import sqrt
from scipy.optimize import newton

import rayoptics.optical.model_constants as mc

from rayoptics.raytr import trace
from rayoptics.raytr import traceerror as terr

# label for coordinate chooser
xy_str = 'xy'


def set_ape(opm):
    """ From existing fields and vignetting, calculate clear apertures. 
    
    This function modifies the max_aperture maintained by the list of
    :class:`~.interface.Interface` in the 
    :class:`~.sequential.SequentialModel`. For each interface, the smallest 
    aperture that will pass all of the (vignetted) boundary rays, for each 
    field, is chosen.
    
    The change of the apertures is propagated to the 
    :class:`~.elements.ElementModel` via 
    :meth:`~.elements.ElementModel.sync_to_seq`.
    """
    rayset = trace.trace_boundary_rays(opm, use_named_tuples=True)

    for i, ifc in enumerate(opm['sm'].ifcs):
        max_ap = -1.0e+10
        update = True
        for f in rayset:
            for p in f:
                ray = p.ray
                if len(ray) > i:
                    ap = sqrt(ray[i].p[0]**2 + ray[i].p[1]**2)
                    if ap > max_ap:
                        max_ap = ap
                else:  # ray failed before this interface, don't update
                    update = False
        if update:
            ifc.set_max_aperture(max_ap)

    # sync the element model with the new clear apertures
    opm['em'].sync_to_seq(opm['sm'])


def set_vig(opm):
    """ From existing fields and clear apertures, calculate vignetting. """
    osp = opm['osp']
    for fi in range(len(osp['fov'].fields)):
        fld, wvl, foc = osp.lookup_fld_wvl_focus(fi)
        # print(f"field {fi}:")
        calc_vignetting_for_field(opm, fld, wvl)


def calc_vignetting_for_field(opm, fld, wvl):
    """Calculate and set the vignetting parameters for `fld`. """
    pupil_starts = opm['osp']['pupil'].pupil_rays[1:]
    vig_factors = [0.]*4
    for i in range(4):
        xy = i//2
        start = pupil_starts[i]
        vig, last_indx, ray_pkg = calc_vignetted_ray(opm, xy, start, fld, wvl)
        # print(f"ray: ({start[0]:2.0f}, {start[1]:2.0f}), vig={vig:8.4f}, "
        #       f"limited at ifcs[{last_indx}]")
        vig_factors[i] = vig

    # update the field's vignetting factors
    fld.vux = vig_factors[0]
    fld.vlx = vig_factors[1]
    fld.vuy = vig_factors[2]
    fld.vly = vig_factors[3]


def calc_vignetted_ray(opm, xy, start_dir, fld, wvl, max_iter_count=10):
    """ Find the limiting aperture and return the vignetting factor. 

    Args:
        opm: :class:`~.OpticalModel` instance
        xy: 0 or 1 depending on x or y axis as the pupil direction
        start_dir: the unit length starting pupil coordinates, e.g [1., 0.]. 
                   This establishes the radial direction of the ray iteration.
        fld: :class:`~.Field` point for wave aberration calculation
        wvl: wavelength of ray (nm)
        max_iter_count: fail-safe limit on aperture search

    Returns:
        (**vig**, **last_indx**, **ray_pkg**)

        - **vig** - vignetting factor
        - **last_indx** - the index of the limiting interface
        - **ray_pkg** - the vignetting-limited ray
 
    """
    rel_p1 = np.array(start_dir)
    sm = opm['sm']
    still_iterating = True
    last_indx = None
    iter_count = 0  # safe guard against runaway iteration
    while still_iterating and iter_count<max_iter_count:
        iter_count += 1
        try:
            ray_pkg = trace.trace_base(opm, rel_p1, fld, wvl, 
                                       apply_vignetting=False, 
                                       check_apertures=True)

        except terr.TraceError as te:
            indx = te.surf
            ray_pkg = te.ray_pkg
            # print(f"{xy_str[xy]} = {rel_p1[xy]:10.6f}: blocked at {indx}")
            if indx == last_indx:
                still_iterating = False
            else:
                r_target = sm.ifcs[indx].surface_od()
                rel_p1 = iterate_pupil_ray(opm, indx, xy, rel_p1[xy], r_target, 
                                           fld, wvl)
                still_iterating = True
                last_indx = indx
        else:  # ray successfully traced.
            # print(f"{xy_str[xy]} = {rel_p1[xy]:10.6f}: passed")
            if last_indx is not None:
                # fall through and exit
                still_iterating = False
            else: # this is the first time through
                # iterate to find the ray that goes through the edge
                # of the stop surface
                indx = stop_indx = sm.stop_surface
                if stop_indx is not None:
                    r_target = sm.ifcs[stop_indx].surface_od()
                    rel_p1 = iterate_pupil_ray(opm, indx, xy, rel_p1[xy], 
                                               r_target, fld, wvl)
                    still_iterating = True
                    last_indx = indx
                else: # floating stop, exit
                    still_iterating = False

    vig = 1.0 - (rel_p1[xy]/start_dir[xy])
    return vig, last_indx, ray_pkg


def iterate_pupil_ray(opt_model, indx, xy, start_r0, r_target, fld, wvl, **kwargs):
    """ iterates a ray to r_target on interface indx, returns aim points on
    the paraxial entrance pupil plane

    If indx is None, i.e. a floating stop surface, returns r_target.

    If the iteration fails, a :class:`~.traceerror.TraceError` will be raised

    Args:
        opm: :class:`~.OpticalModel` instance
        indx: index of interface whose edge is the iteration target
        xy: 0 or 1 depending on x or y axis as the pupil direction
        start_r0: iteration starting point
        r_target: clear aperture radius that is the iteration target.
        fld: :class:`~.Field` point for wave aberration calculation
        wvl: wavelength of ray (nm)

    Returns:
        start_coords: pupil coordinates for ray thru r_target on ifc indx.

    """

    def r_pupil_coordinate(xy_coord, *args):
        opt_model, indx, xy, fld, wvl, r_target = args

        rel_p1 = np.array([0., 0.])
        rel_p1[xy] = xy_coord
        try:
            ray_pkg = trace.trace_base(opt_model, rel_p1, fld, wvl, 
                                       apply_vignetting=False, 
                                       check_apertures=False)
            ray = ray_pkg[0]
        except terr.TraceMissedSurfaceError as ray_miss:
            ray = ray_miss.ray_pkg
            if ray_miss.surf <= indx:
                raise ray_miss
        except terr.TraceTIRError as ray_tir:
            ray = ray_tir.ray_pkg
            if ray_tir.surf < indx:
                raise ray_tir
        r_ray = sqrt(ray[indx][mc.p][0]**2 + ray[indx][mc.p][1]**2)
#        print(xy_coord, r_ray, r_target, r_ray - r_target)
        return r_ray - r_target

    start_coords = np.array([0., 0.])
    if indx is not None:
        logging.captureWarnings(True)
        try:
            start_r, results = newton(r_pupil_coordinate, start_r0,
                                      args=(opt_model, indx, xy,
                                            fld, wvl, r_target), tol=1e-6,
                                      disp=False, full_output=True)
        except RuntimeError as rte:
            # if we come here, set start_r to a RuntimeResults object
            start_r = results.root
        except terr.TraceError:
            start_r = 0.0
        start_coords[xy] = start_r
        # print(f"converged={results.converged} in {results.iterations} iterations")

    else:  # floating stop surface - use entrance pupil for aiming
        start_coords[xy] = r_target

    return start_coords
