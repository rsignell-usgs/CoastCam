import imageio
import numpy as np
from scipy.interpolate import RectBivariateSpline
from scipy.ndimage.morphology import distance_transform_edt

#from .calibration import CameraCalibration #CRS


class TargetGrid(object):
    """Grid generated to georectify image.
    
    CRS modified to make endpoints inclusive
    Notes:
        - Used to maps points in world coordinates to pixels
        - The limits should be specified in local coordinates using the same
          coordinates and units as camera calibrations.

    Args:
        xlims (ndarray) - min and max (inclusive) in the x-direction (e.g. [-50, 650])
        ylims (ndarray) - min and max (inclusive) in the y-direction (e.g. [0, 2501])
        dx (float) - resolution of grid in x direction (same units as camera calibration)
        dy (float) - resolution of grid in y direction (same units as camera calibration)
        z (float) - static value to estimate elevation at everypoint in the x, y grid

    Attributes:
        X (np.ndarray): Local grid coordinates in x-direction.
        Y (np.ndarray): Local grid coordinates in y-direction.
        Z (np.ndarray): Local grid coordinates in z-direction.
        xyz (np.ndarray): The grid where pixels are compiled from images for rectification.
    """
    def __init__(self, xlims, ylims, dx=1, dy=1, z=-0.91):
        x = np.arange(xlims[0], xlims[1]+dx, dx)
        y = np.arange(ylims[0], ylims[1]+dx, dy)
        self.X, self.Y = np.meshgrid(x, y)
        self.Z = np.zeros_like(self.X) + z
        self.xyz = self._xyz_grid()

    def _xyz_grid(self):
        x = self.X.copy().T.flatten()
        y = self.Y.copy().T.flatten()
        z = self.Z.copy().T.flatten()
        return np.vstack((x, y, z)).T


class Rectifier(object):
    """Georectifies an oblique image given RectifierGrid and ncolors.

    Note:
        Derived from example code for georectifying mini-Argus imagery.

    Attributes:
        target_grid (TargetGrid): Params and grid used to create georectified image.
        camera_calibration (CameraCalibration): CameraCalibration including intrinsic (LCP) and extrinsic (Beta) coefficients.
        ncolors (int): Number of colors in camera images.
        U (np.ndarray): horizontal image coordinates
        V (np.ndarray): vertical image coordinates (increasing down)
    """
    def __init__(self, target_grid, ncolors=3):
        self.target_grid = target_grid
        self.ncolors = ncolors

    def find_UV(self, calibration):
        # get UV for pinhole camera
        print("target_grid.xyz: ",np.shape(self.target_grid.xyz))
        tmp = np.vstack((
            self.target_grid.xyz.T,
            np.ones((len(self.target_grid.xyz),))
        ))
        print("tmp:",np.shape(tmp))
        UV = np.matmul(calibration.P, tmp)
        # make homogenous
        div = np.tile(UV[2, :], (3, 1))
        UV = UV / div

        # distort pixel locations to match camera distortion
        U, V = self._distort_UV_BB(UV, calibration)

        return U, V

    def _distort_UV_BB(self, UV: np.ndarray, calibration: CameraCalibration):
        """
        This version is based on Brittany Bruder's version and does not need
        the extra intrinsic values r, fr, x, y, dx, and dy
        """
        NU = calibration.lcp['NU']
        NV = calibration.lcp['NV']
        c0U = calibration.lcp['c0U']
        c0V = calibration.lcp['c0V']
        fx = calibration.lcp['fx']
        fy = calibration.lcp['fy']
        d1 = calibration.lcp['d1']
        d2 = calibration.lcp['d2']
        d3 = calibration.lcp['d3']
        t1 = calibration.lcp['t1']
        t2 = calibration.lcp['t2']
        print("Shape of UV")
        print(np.shape(UV))
        u = UV[0, :]
        v = UV[1, :]

        print("shape of u and v")
        print(np.shape(u),np.shape(v))

        # normalize distances
        x = (u - c0U) / fx
        y = (v - c0V) / fy
        # radial distortion
        r2 = x*x + y*y
        fr = 1. + d1*r2 + d2*r2*r2 + d3*r2*r2*r2
        # tangential distorion
        dx=2.*t1*x*y + t2*(r2+2.*x*x)
        dy=t1*(r2+2.*y*y) + 2.*t2*x*y
        # apply correction, answer in chip pixel units
        xd = x*fr + dx
        yd = y*fr + dy
        Ud = xd*fx+c0U
        Vd = yd*fy+c0V

        flag = np.ones_like(Ud)
        # find negative UV coordinates
        flag[np.where( Ud<0.)]=0.
        flag[np.where( Vd<0.)]=0.
        # find UVd coordinates greater than image size
        flag[np.where( Ud>=NU)]=0.
        flag[np.where( Vd>=NV)]=0.

        # Determine if Tangential Distortion is within Range
        #  Find Maximum possible tangential distortion at corners
        Um=np.array((0, 0, NU, NU))
        Vm=np.array((0, NV, NV, 0))

        # Normalization
        xm = (Um-c0U)/fx
        ym = (Vm-c0V)/fy
        r2m = xm*xm + ym*ym

        # Tangential Distortion
        dxm=2.*t1*xm*ym + t2*(r2m+2.*xm*xm)
        dym=t1*(r2m+2.*ym*ym) + 2.*t2*xm*ym

        # Find Values Larger than those at corners
        flag[np.where(np.abs(dy)>np.max(np.abs(dym)))]=0.
        flag[np.where(np.abs(dx)>np.max(np.abs(dxm)))]=0.

        DU = Ud.reshape(self.target_grid.X.shape, order='F')
        DV = Vd.reshape(self.target_grid.Y.shape, order='F')

        return (DU, DV), flag

    def _distort_UV(self, UV: np.ndarray, calibration: CameraCalibration):
        """Distorts pixel locations to match camera distortion.

        Notes:
            - Derived from distortCaltech.m from Coastal Imaging Research Network - Oregon State University
            - Originally derived from Caltech lens distortion manuals
            - Required to locate correct pixel values for each position in distorted imaage for DJI phantom.

        Arguments:
            UV (np.ndarray): Coordinates of each pixel (undistorted).
            calibration (CameraCalibration): CameraCalibration including intrinsic (lcp) and extrinsic (beta) coefficients.

        Returns:
            distorted_uv (tuple): U and V arrays distorted to match camera distortion.
        """
        # calculate normalized image coordinates
        # - undistorted U-V to x-y space (normalized image coordinates)
        # - translated image center divded by focal length in pixels
        u = UV[0, :]
        v = UV[1, :]
        x = (u - calibration.lcp['c0U']) / calibration.lcp['fx']
        y = (v - calibration.lcp['c0V']) / calibration.lcp['fy']

        # distortion found based on large format units
        r2 = x*x + y*y
        fr = np.interp(
            np.sqrt(r2),
            calibration.lcp['r'],
            calibration.lcp['fr'],
            right=np.nan
        )

        # get values for dx and dy at grid locations
        # - use linear in x + y direction
        # - this method will extrapolate, so we mask out values beyond
        #   source grid with nans to match matlab version
        mask_x = np.logical_or(
            x <= calibration.lcp['x'].min(),
            x >= calibration.lcp['x'].max()
        )
        mask_y = np.logical_or(
            y <= calibration.lcp['y'].min(),
            y >= calibration.lcp['y'].max()
        )
        mask = np.logical_or(
            mask_x,
            mask_y
        )

        dx_rbs = RectBivariateSpline(
            calibration.lcp['y'],
            calibration.lcp['x'],
            calibration.lcp['dx'],
            kx=1,
            ky=1
        )
        dx = dx_rbs.ev(y, x)
        dx[mask] = np.nan
        dy_rbs = RectBivariateSpline(
            calibration.lcp['y'],
            calibration.lcp['x'],
            calibration.lcp['dy'],
            kx=1,
            ky=1
        )
        dy = dy_rbs.ev(y, x)
        dy[mask] = np.nan

        x2 = x*fr + dx
        y2 = y*fr + dy

        # results in chip pixel units
        ud = x2 * calibration.lcp['fx'] + calibration.lcp['c0U']
        vd = y2 * calibration.lcp['fy'] + calibration.lcp['c0V']
        # reshape to match matlab version
        DU = ud.reshape(self.target_grid.X.shape, order='F')
        DV = vd.reshape(self.target_grid.Y.shape, order='F')

        return (DU, DV)

    def get_pixels(self, DU, DV, image):
        """Return pixel values for each xyz point from the image

        Arguments:
            DU (np.ndarray): Pixel location in camera orientation and coordinate system
            DV (np.ndarray): Pixel location in cmaera orientation and coorindate system

        Returns:
            K (np.ndarray): Pixel intensity for each point in the image
        """
        K = np.zeros((
            self.target_grid.X.shape[0],
            self.target_grid.X.shape[1],
            self.ncolors
        ))
        for c, _ in enumerate(['r', 'b', 'g']):
            rbs = RectBivariateSpline(
                # use this range to match matlab exactly
                np.arange(1, image.shape[0] + 1),
                np.arange(1, image.shape[1] + 1),
                image[:, :, c],
                kx=1,
                ky=1
            )
            K[:, :, c] = rbs.ev(DV, DU)

        # mask out values out of range like matlab
        # avoid runtime nan comparison warning (UV, DV already have nans)
        with np.errstate(invalid='ignore'):
            mask_u = np.logical_or(
                DU <= 1,
                DU >= image.shape[1]
            )
            mask_v = np.logical_or(
                DV <= 1,
                DV >= image.shape[0]
            )
        mask = np.logical_or(
            mask_u,
            mask_v
        )
        K[mask] = np.nan

        return K

    def assemble_image_weights(self, K):
        """Return weight matrix W used for image merging.

        Notes:
            - Calculates Euclidean distance for each entry to nearest non-zero pixel value
            - edt: Euclidean Distance Transform

        Arguments:
            K (np.ndarray): Pixel intensity

        Returns:
            W (np.ndarray): Pixel weights used for merging images
        """
        # NaN in K indicates no pixel value at that location
        # edt finds euclidean distance from no value to closest value
        # so, find the nans, then invert so it works with the function
        W = distance_transform_edt(~np.isnan(K[:, :, 0]))

        # Not sure when this would happend, but I'm including it because it's in the MATLAB code
        if np.isinf(np.max(W)):
            W[:] = 1

        W = W / np.max(W)
        W[W == 0] = np.nan

        return W

    def apply_weights_to_pixels(self, K, W):
        """Return pixel intensities (K) weighted by W.

        Arguments:
            K (np.ndarray): Pixel intensity
            W (np.ndarray): Pixel weights used for merging images

        Returns:
            K_weighted(np.ndarray): Pixel intensity weighted for merging
        """
        W_nonan = W.copy()
        W_nonan[np.isnan(W_nonan)] = 0
        K_weighted = K*W_nonan[:, :, np.newaxis]

        return K_weighted

    def rectify_images(self, metadata, image_files, intrinsic_cal_list, extrinsic_cal_list, local_origin, progress_callback=None):
        """Given list of image paths from N cameras, return a georectified image (ndarray)

        Arguments:
            image_files (list): List of image files.
            camera_calibration_files (list): List of calibrations for cameras used to get image_files.
            progress_callback (callable): Optional; callable taking one parameter, an integer of progress 0-100

        Returns:
            M (np.ndarray): Georectified images merged from supplied images.
        """
        M = np.tile(
            np.zeros_like(self.target_grid.X[:, :, np.newaxis]),
            (self.ncolors,)
        )
        totalW = M.copy()

        total_pairs = len(image_files)
        steps_per_pair = 3
        total_steps = total_pairs * steps_per_pair
        if progress_callback is None:
            progress_callback = lambda x: x

        for cur_idx, (image_file, intrinsic_cal, extrinsic_cal) in enumerate(zip(image_files, intrinsic_cal_list, extrinsic_cal_list)):
            pct_base = cur_idx * steps_per_pair

            # load camera calibration file and find pixel locations
            camera_calibration = CameraCalibration(metadata, intrinsic_cal, extrinsic_cal, local_origin)
            U, V = self.find_UV(camera_calibration)

            progress_callback(int((pct_base + 1) / total_steps * 100))

            # load image and apply weights to pixels
            image = imageio.imread(image_file)
            print(np.shape(image),print(np.shape(V)))
            K = self.get_pixels(U, V, image)
            W = self.assemble_image_weights(K)
            K_weighted = self.apply_weights_to_pixels(K, W)

            progress_callback(int((pct_base + 2) / total_steps * 100))

            # add up weights and pixel itensities
            W[np.isnan(W)] = 0
            totalW = totalW + W[:, :, np.newaxis]
            K_weighted[np.isnan(K_weighted)] = 0
            M = M + K_weighted

            progress_callback(int((pct_base + 3) / total_steps * 100))

        # stop divide by 0 warnings
        with np.errstate(invalid='ignore'):
            M = M / totalW

        return M