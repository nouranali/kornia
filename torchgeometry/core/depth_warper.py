from typing import Optional, Iterable, Union
import torch
import torch.nn as nn
import torch.nn.functional as F

from torchgeometry.core.transformations import relative_pose
from torchgeometry.core.conversions import transform_points
from torchgeometry.core.conversions import convert_points_to_homogeneous
from torchgeometry.core.pinhole import PinholeCamera, PinholeCamerasList
from torchgeometry.core.pinhole import cam2pixel, pixel2cam
from torchgeometry.utils import create_meshgrid, normalize_pixel_coordinates


__all__ = [
    "depth_warp",
    "DepthWarper",
]


class DepthWarper(nn.Module):
    """Warps a patch by depth.

    .. math::
        H_{src}^{dst} = K_{dst} * T_{src}^{dst} * K_{src}^{-1}

        I_{src} = \\omega(I_{dst}, H_{src}^{dst}, D_{src})

    Args:
        pinholes_dst (Iterable[PinholeCamera]): The pinhole models for the
          destination frame.
        height (int): The height of the image to warp.
        width (int): The width of the image to warp.

    """

    def __init__(self,
                 pinholes_dst: Iterable[PinholeCamera],
                 height: int, width: int):
        super(DepthWarper, self).__init__()
        self.width: int = width
        self.height: int = height
        self.eps = 1e-6

        self._pinhole_dst: PinholeCamerasList = PinholeCamerasList(pinholes_dst)
        self._pinhole_src: Union[None, PinholeCamera] = None
        self._dst_proj_src: Union[None, torch.Tensor] = None

        self.grid: torch.Tensor = self._create_meshgrid(height, width)

    @staticmethod
    def _create_meshgrid(height: int, width: int) -> torch.Tensor:
        grid: torch.Tensor = create_meshgrid(
            height, width, normalized_coordinates=False)  # 1xHxWx2
        return convert_points_to_homogeneous(grid)  # append ones to last dim

    def compute_projection_matrix(self, pinhole_src: PinholeCamera):
        if not isinstance(pinhole_src, PinholeCamera):
            raise TypeError("Argument pinhole_src expected to be of class "
                            "PinholeCamera. Got {}".format(type(pinhole_src)))
        # compute the relative pose between the non reference and the reference
        # camera frames.
        batch_size: int = pinhole_src.batch_size
        num_cameras: int = self._pinhole_dst.num_cameras

        extrinsics_src: torch.Tensor = pinhole_src.extrinsics[:, None].expand(
            -1, num_cameras, -1, -1)  # BxNx4x4
        extrinsics_src = extrinsics_src.contiguous().view(-1, 4, 4)  # (B*N)x4x4
        extrinsics_dst: torch.Tensor = self._pinhole_dst.extrinsics.view(
            -1, 4, 4)  # (B*N)x4x4
        dst_trans_src: torch.Tensor = relative_pose(
            extrinsics_src, extrinsics_dst)

        # compute the projection matrix between the non reference cameras and
        # the reference.
        intrinsics_dst: torch.Tensor = self._pinhole_dst.intrinsics.view(
            -1, 4, 4)
        dst_proj_src: torch.Tensor = torch.matmul(
            intrinsics_dst, dst_trans_src)

        # update class member
        self._pinhole_src = pinhole_src
        self._dst_proj_src = dst_proj_src.view(
            pinhole_src.batch_size, -1, 4, 4)
        return self

    def _compute_projection(self, x, y, invd):
        point = torch.FloatTensor([[[x], [y], [1.0], [invd]]])
        flow = torch.matmul(
            self._dst_proj_src[:, 0], point.to(self._dst_proj_src.device))
        z = 1. / flow[:, 2]
        x = (flow[:, 0] * z)
        y = (flow[:, 1] * z)
        return torch.cat([x, y], 1)

    def compute_subpixel_step(self):
        """This computes the required inverse depth step to achieve sub pixel
        accurate sampling of the depth cost volume, per camera.

        Szeliski, Richard, and Daniel Scharstein.
        "Symmetric sub-pixel stereo matching." European Conference on Computer
        Vision. Springer Berlin Heidelberg, 2002.
        """
        delta_d = 0.01
        xy_m1 = self._compute_projection(self.width / 2, self.height / 2,
                                         1.0 - delta_d)
        xy_p1 = self._compute_projection(self.width / 2, self.height / 2,
                                         1.0 + delta_d)
        dx = torch.norm((xy_p1 - xy_m1), 2, dim=-1) / 2.0
        dxdd = dx / (delta_d)  # pixel*(1/meter)
        # half pixel sampling, we're interested in the min for all cameras
        return torch.min(0.5 / dxdd)

    # compute grids
    def warp_grid(self, depth_src: torch.Tensor) -> torch.Tensor:
        """Computes a grid for warping a given the depth from the reference
        pinhole camera.

        The function `compute_projection_matrix` has to be called beforehand in
        order to have precomputed the relative projection matrices encoding the
        relative pose and the intrinsics between the reference and a non
        reference camera.
        """
        # TODO: add type and value checkings
        if self._dst_proj_src is None or self._pinhole_src is None:
            raise ValueError("Please, call compute_projection_matrix.")

        if len(depth_src.shape) != 4:
            raise ValueError("Input depth_src has to be in the shape of "
                             "Bx1xHxW. Got {}".format(depth_src.shape))

        # unpack depth attributes
        batch_size, _, height, width = depth_src.shape
        device: torch.device = depth_src.device
        dtype: torch.dtype = depth_src.dtype

        # expand the base coordinate grid according to the input batch size
        pixel_coords: torch.Tensor = self.grid.to(device).to(dtype).expand(
            batch_size, -1, -1, -1)  # BxHxWx3

        # reproject the pixel coordinates to the camera frame
        cam_coords_src: torch.Tensor = pixel2cam(
            depth_src,
            self._pinhole_src.intrinsics_inverse().to(dtype),
            pixel_coords)  # BxHxWx3

        # create views from tensors to match with cam2pixel expected shapes
        cam_coords_src = cam_coords_src[:, None].expand(
            -1, self._pinhole_dst.num_cameras, -1, -1, -1)  # BxNxHxWx3
        cam_coords_src = cam_coords_src.contiguous().view(
            -1, height, width, 3)                          # (B*N)xHxWx3
        dst_proj_src: torch.Tensor = self._dst_proj_src.view(-1, 4, 4)

        # reproject the camera coordinates to the pixel
        pixel_coords_src: torch.Tensor = cam2pixel(
            cam_coords_src, dst_proj_src.to(dtype))  # (B*N)xHxWx2

        # normalize between -1 and 1 the coordinates
        pixel_coords_src_norm: torch.Tensor = normalize_pixel_coordinates(
            pixel_coords_src, self.height, self.width)
        return pixel_coords_src_norm

    def forward(
            self,
            depth_src: torch.Tensor,
            patch_dst: torch.Tensor) -> torch.Tensor:
        """Warps a tensor from destination frame to reference given the depth
        in the reference frame.

        Args:
            depth_src (torch.Tensor): The depth in the reference frame. The
              tensor must have a shape :math:`(B, 1, H, W)`.
            patch_dst (torch.Tensor): The patch in the destination frame. The
              tensor must have a shape :math:`(B, C, H, W)`.

        Return:
            torch.Tensor: The warped patch from destination frame to reference.

        Shape:
            - Output: :math:`(N, C, H, W)` where C = number of channels.

        Example:
            >>> # pinholes camera models
            >>> pinhole_dst = tgm.PinholeCamera(...)
            >>> pinhole_src = tgm.PinholeCamera(...)
            >>> # create the depth warper, compute the projection matrix
            >>> warper = tgm.DepthWarper([pinhole_dst, ], height, width)
            >>> warper.compute_projection_matrix(pinhole_src)
            >>> # warp the destionation frame to reference by depth
            >>> depth_src = torch.ones(1, 1, 32, 32)  # Nx1xHxW
            >>> image_dst = torch.rand(1, 3, 32, 32)  # NxCxHxW
            >>> image_src = warper(depth_src, image_dst)    # NxCxHxW
        """
        return F.grid_sample(patch_dst, self.warp_grid(depth_src))


# functional api

def depth_warp(pinholes_dst: Iterable[PinholeCamera],
               pinhole_src: PinholeCamera,
               depth_src: torch.Tensor,
               patch_dst: torch.Tensor,
               height: int, width: int):
    r"""Function that warps a tensor from destination frame to reference
    given the depth in the reference frame.

    See :class:`~torchgeometry.DepthWarper` for details.

    Example:
        >>> # pinholes camera models
        >>> pinhole_dst = tgm.PinholeCamera(...)
        >>> pinhole_src = tgm.PinholeCamera(...)
        >>> # warp the destionation frame to reference by depth
        >>> depth_src = torch.ones(1, 1, 32, 32)  # Nx1xHxW
        >>> image_dst = torch.rand(1, 3, 32, 32)  # NxCxHxW
        >>> image_src = tgm.depth_warp([pinhole_dst, ], pinhole_src,
        >>>     depth_src, image_dst, height, width)  # NxCxHxW
    """
    warper = DepthWarper(pinholes_dst, height, width)
    warper.compute_projection_matrix(pinhole_src)
    return warper(depth_src, patch_dst)
