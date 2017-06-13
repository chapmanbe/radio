# pylint: disable=no-member
# pylint: disable=no-name-in-module
# pylint: disable=arguments-differ
"""Contains class CTImagesMaskedBatch for storing masked Ct-scans."""
import os
from binascii import hexlify
import logging
import shutil
import blosc
import numpy as np
from numba import njit
import SimpleITK as sitk
from .ct_batch import CTImagesBatch
from .mask import make_mask_numba
from .resize import resize_patient_numba
from .dataset_import import action, inbatch_parallel, any_action_failed


LOGGING_FMT = (u"%(filename)s[LINE:%(lineno)d]#" +
               "%(levelname)-8s [%(asctime)s]  %(message)s")
logging.basicConfig(format=LOGGING_FMT, level=logging.DEBUG)

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@njit(nogil=True)
def get_nodules_numba(data, positions, size):
    """Fetch nodules from array by array of starting positions.

    This numberized function takes source array with data of shape (n, k, l)
    represented by 3d numpy array with BatchCt data,
    ndarray(p, 3) with starting indices of nodules where p is number
    of nodules and size of type ndarray(3, ) which contains
    sizes of nodules along each axis. The output is 3d ndarray with nodules
    put in CTImagesBatch-compatible skyscraper structure.

    *Note that dtypes of positions and size arrays must be the same.

    Args:
    - data: CTImagesBatch skyscraper represented by 3d numpy array;
    - positions: ndarray(l, 3) of int containing
      nodules' starting indices along [zyx]-axis
      accordingly in ndarray data;
    - size: ndarray(3,) of int containing
      nodules' sizes along each axis;
    """
    out_arr = np.zeros((np.int(positions.shape[0]), size[0], size[1], size[2]))

    n_positions = positions.shape[0]
    for i in range(n_positions):
        out_arr[i, :, :, :] = data[positions[i, 0]: positions[i, 0] + size[0],
                                   positions[i, 1]: positions[i, 1] + size[1],
                                   positions[i, 2]: positions[i, 2] + size[2]]

    return out_arr.reshape(n_positions * size[0], size[1], size[2])


class CTImagesMaskedBatch(CTImagesBatch):
    # TODO change bias in name to offset or smth like this
    """Class for storing masked batch of ct-scans.

    In addition to batch itself, stores mask in
    self.mask as ndarray, origin and spacing dictionaries
    and list with information about nodules in batch.

    new attrs:
        1. mask: ndarray of masks
        2. spacing: dict with keys = self.indices
            stores distances between pixels in mm for patients
            order is x, y, z
        3. origin: dict with keys = self.indices
            stores world coords of [0, 0, 0]-pixel of data for
            all patients
        4. nodules_info: list with information about nodule; each nodule
            represented by instance of Nodule class

    Important methods:
        1. load_mask(self, nodules_df, num_threads=8)
            function for
            loading masks from dataframe with nodules
            multithreading is supported
        2. resize(self, num_x_new=256, num_y_new=256,
                  num_slices_new=128, order=3, num_threads=8)
            transform shape of all patients to
            (num_slices_new, num_y_new, num_x_new)
            if masks are loaded, they are are also resized

        *Note: spacing, origin are recalculated when resize is executed
            As a result, load_mask can be also executed after resize
    """
    # record array contains the following information about nodules:
    # - self.nodules.nodule_center -- ndarray(num_nodules, 3) centers of
    #   nodules in world coords;
    # - self.nodules.nodule_size -- ndarray(num_nodules, 3) sizes of
    #   nodules along z, y, x in world coord;
    # - self.nodules.img_size -- ndarray(num_nodules, 3) sizes of images of
    #   patient data corresponding to nodules;
    # - self.nodules.offset -- ndarray(num_nodules, 3) of biases of
    #   patients which correspond to nodules;
    # - self.nodules.spacing -- ndarray(num_nodules, 3) of spacinf attribute
    #   of patients which correspond to nodules;
    # - self.nodules.origin -- ndarray(num_nodules, 3) of origin attribute
    #   of patients which correspond to nodules;
    nodules_dtype = np.dtype([('patient_pos', np.int, 1),
                              ('offset', np.int, (3,)),
                              ('img_size', np.int, (3,)),
                              ('nodule_center', np.float, (3,)),
                              ('nodule_size', np.float, (3,)),
                              ('spacing', np.float, (3,)),
                              ('origin', np.float, (3,))])

    @staticmethod
    def make_indices(size):
        """Generate list of batch indices of given size.

        Take number of indices as input parameter size and
        generates list of random indices of length size.

        Args:
        - size: size of list with indices;
        """
        return [CTImagesMaskedBatch.make_filename() for i in range(size)]

    def __init__(self, index):
        """Initialization of CTImagesMaskedBatch.

        Initialize CTImagesMaskedBatch with index.
        """
        super().__init__(index)
        self.mask = None
        self.nodules = None

    @action
    def load(self, source=None, fmt='dicom', bounds=None,
             origin=None, spacing=None, nodules=None, mask=None):
        """Load data in masked batch of patients.

        Args:
        - source: source array with skyscraper, needed if fmt is 'ndarray';
        - fmt: type of source data; possible values are 'raw' and 'ndarray';
        Returns:
        - self;

        Examples:
        >>> index = FilesIndex(path="/some/path/*.mhd, no_ext=True")
        >>> batch = CTImagesMaskedBatch(index)
        >>> batch.load(fmt='raw')

        >>> batch.load(src=source_array, fmt='ndarray', bounds=bounds,
        ...            origin=origin_dict, spacing=spacing_dict)
        """
        params = dict(source=source, bounds=bounds,
                      origin=origin, spacing=spacing)
        if fmt == 'ndarray':
            self._init_data(**params)
            self.nodules = nodules
            self.mask = mask
        else:
            # TODO check this
            super().load(fmt=fmt, **params)
        return self

    @action
    @inbatch_parallel(init='indices', post='_post_default',
                      target='async', update=False)
    async def dump(self, patient, dst, src="data", fmt="blosc"):
        """Dump mask or source data on specified path and format.mro.

        Dump data or mask in CTIMagesMaskedBatch on specified path and format.
        Create folder corresponing to each patient.

        example:
            # initialize batch and load data
            ind = ['1ae34g90', '3hf82s76', '2ds38d04']
            batch.load(...)
            batch.create_mask(...)
            batch.dump(dst='./data/blosc_preprocessed', src='data')
            # the command above creates files

            # ./data/blosc_preprocessed/1ae34g90/data.blk
            # ./data/blosc_preprocessed/3hf82s76/data.blk
            # ./data/blosc_preprocessed/2ds38d04/data.blk
            batch.dump(dst='./data/blosc_preprocessed_mask', src='mask')
        """
        if fmt != 'blosc':
            raise NotImplementedError('Dump to {} is ' +
                                      'not implemented yet'.format(fmt))
        if src == 'data':
            data_to_dump = self.get_image(patient)
            pat_attrs = self.get_attrs(patient)
        elif src == 'mask':
            data_to_dump = self.get_mask(patient)
            pat_attrs = self.get_attrs(patient)
        return await self.dump_data_attrs(data_to_dump, pat_attrs, patient, dst)

    def get_mask(self, index):
        """Get view on patient data's mask.

        This method takes position of patient in self or his index
        and returns view on patient data's mask.

        Args:
        - index: can be either position of patient in self._data
        or index from self.index;

        Return:
        - ndarray(Nz, Ny, Nz): view on patient data's mask array;
        """
        if self.mask is None:
            return None
        pos = self._get_verified_pos(index)
        return self.mask[self.lower_bounds[pos]: self.upper_bounds[pos], :, :]

    @property
    def num_nodules(self):
        """Get number of nodules in CTImagesMaskedBatch.

        This property returns the number
        of nodules in CTImagesMaskedBatch. If fetch_nodules_info
        method has not been called yet returns -1.
        """
        if self.nodules is not None:
            return self.nodules.patient_pos.shape[0]
        else:
            return 0

    @action
    def fetch_nodules_info(self, nodules_df, update=False):
        """Extract nodules' info from nodules_df into attribute self.nodules.

        This method fetch info about all nodules in batch
        and put them in numpy record array which can be accessed outside
        the class by self.nodules. Record array self.nodules
        has 'spacing', 'origin', 'img_size' and 'bias' properties, each
        represented by ndarray(num_nodules, 3) referring to spacing, origin,
        image size and bound of patients which correspond to fetched nodules.
        Record array self.nodules also contains attributes 'center' and 'size'
        which contain information about center and size of nodules in
        world coordinate system, each of these properties is represented by
        ndarray(num_nodules, 3). Finally, self.nodules.patient_pos refers to
        positions of patients which correspond to stored nodules.
        Object self.nodules is used by some methods, for example, create mask
        or sample nodule batch, to perform transform from world coordinate
        system to pixel one.
        """
        if self.nodules is not None and not update:
            logger.warning("Nodules have already been extracted. " +
                           "Put update argument as True for refreshing")
            return self
        nodules_df = nodules_df.set_index('seriesuid')

        unique_indices = nodules_df.index.unique()
        inter_index = np.intersect1d(unique_indices, self.indices)
        nodules_df = nodules_df.loc[inter_index,
                                    ["coordZ", "coordY",
                                     "coordX", "diameter_mm"]]

        num_nodules = nodules_df.shape[0]
        self.nodules = np.rec.array(np.zeros(num_nodules,
                                             dtype=self.nodules_dtype))
        counter = 0
        for pat_id, coordz, coordy, coordx, diam in nodules_df.itertuples():
            pat_pos = self.index.get_pos(pat_id)
            self.nodules.patient_pos[counter] = pat_pos
            self.nodules.nodule_center[counter, :] = np.array([coordz,
                                                        coordy,
                                                        coordx])
            self.nodules.nodule_size[counter, :] = np.array([diam, diam, diam])
            counter += 1

        self._refresh_nodules_info()
        return self

    # TODO think about another name of method
    def _fit_into_bounds(self, size, variance=None):
        """Fetch start pixel coordinates of all nodules.

        This method returns start pixel coordinates of all nodules
        in batch. Note that all nodules are considered to have the
        fixed size defined by argument size: if nodule is out of
        patient's 3d image bounds than it's center is shifted.

        Args:
        - size: list, tuple of numpy array of length 3 with pixel
        size of nodules;
        - covariance: ndarray(3, ) diagonal elements
        of multivariate normal distribution used for sampling random shifts
        along [z, y, x] correspondingly;
        """
        size = np.array(size, dtype=np.int)

        center_pix = np.abs(self.nodules.nodule_center -
                            self.nodules.origin) / self.nodules.spacing
        start_pix = (np.rint(center_pix) - np.rint(size / 2))
        if variance is not None:
            start_pix += np.random.multivariate_normal(np.zeros(3),
                                                       np.diag(variance),
                                                       self.nodules.patient_pos.shape[0])
        end_pix = start_pix + size

        bias_upper = np.maximum(end_pix - self.nodules.img_size, 0)
        start_pix -= bias_upper
        end_pix -= bias_upper

        bias_lower = np.maximum(-start_pix, 0)
        start_pix += bias_lower
        end_pix += bias_lower

        return (start_pix + self.nodules.offset).astype(np.int)


    @action
    def create_mask(self):
        """Load mask data for using nodule's info.

        Load mask into self.mask using info in attribute self.nodules_info.
        *Note: nodules info must be loaded before the call of this method.
        """
        if self.nodules is None:
            logger.warning("Info about nodules location must " +
                           "be loaded before calling this method. " +
                           "Nothing happened.")
        self.mask = np.zeros_like(self.data)

        center_pix = np.abs(self.nodules.nodule_center -
                            self.nodules.origin) / self.nodules.spacing
        start_pix = (center_pix - np.rint(self.nodules.nodule_size /
                                          self.nodules.spacing / 2))
        start_pix = np.rint(start_pix).astype(np.int)
        make_mask_numba(self.mask, self.nodules.offset,
                        self.nodules.img_size + self.nodules.offset, start_pix,
                        np.rint(self.nodules.nodule_size / self.nodules.spacing))

        return self

    def fetch_mask_data(self, mask_shape):
        """
        create scaled mask using nodule info from self
        args:
            mask_shape: requiring shape of mask to be created
        return:
            3d-array with mask
        # TODO: one part of code from here repeats create_mask function
            better to unify these two func
        """
        if self.nodules is None:
            logger.warning("Info about nodules location must " +
                           "be loaded before calling this method. " +
                           "Nothing happened.")
        mask = np.zeros(shape=(len(self) * mask_shape[0], ) + tuple(mask_shape[1:]))

        # infer scale factor; assume patients are already resized to equal shapes
        scale_factor = np.asarray(mask_shape) / self.shape[0, :]

        # get rescaled nodule-centers, nodule-sizes, offsets, locs of nod starts
        center_scaled = np.abs(self.nodules.nodule_center - self.nodules.origin) / \
                               self.nodules.spacing * scale_factor
        start_scaled = (center_scaled - scale_factor * self.nodules.nodule_size / \
                                        self.nodules.spacing / 2)
        start_scaled = np.rint(start_scaled).astype(np.int)
        offset_scaled = np.rint(self.nodules.offset * scale_factor).astype(np.int)
        img_size_scaled = np.rint(self.nodules.img_size * scale_factor).astype(np.int)
        nod_size_scaled = (np.rint(scale_factor * self.nodules.nodule_size / 
                            self.nodules.spacing)).astype(np.int)
        # put nodules into mask
        make_mask_numba(mask, offset_scaled, img_size_scaled + offset_scaled,
                        start_scaled, nod_size_scaled)
        # return ndarray-mask
        return mask





    # TODO rename function to sample_random_nodules_positions
    def sample_random_nodules(self, num_nodules, nodule_size):
        """Sample random nodules from CTImagesBatchMasked skyscraper.

        Samples random num_nodules' lower_bounds coordinates
        and stack obtained data into ndarray(l, 3) then returns it.
        First dimension of that array is just an index of sampled
        nodules while second points out pixels of start of nodules
        in BatchCt skyscraper. Each nodule have shape
        defined by parameter size. If size of patients' data along
        z-axis is not the same for different patients than
        NotImplementedError will be raised.

        Args:
        - num_nodules: number of random nodules to sample from BatchCt data;
        - nodule_size: ndarray(3, ) nodule size in number of pixels;

        return
        - ndarray(l, 3) of int that contains information
        about starting positions
        of sampled nodules in BatchCt skyscraper along each axis.
        First dimension is used to index nodules
        while the second one refers to various axes.

        *Note: [zyx]-ordering is used;
        """
        all_indices = np.arange(len(self))
        sampled_indices = np.random.choice(all_indices,
                                           num_nodules, replace=True)

        offset = np.zeros((num_nodules, 3))
        offset[:, 0] = self.lower_bounds[sampled_indices]

        data_shape = self.shape[sampled_indices, :]
        samples = np.random.rand(num_nodules, 3) * (data_shape - nodule_size)
        return np.asarray(samples + offset, dtype=np.int)

    @action
    def sample_nodules(self, batch_size, nodule_size, share=0.8,
                       variance=None, mask_shape=None, if_tensor=False):
        """Fetch random cancer and non-cancer nodules from batch.

        Fetch nodules from CTImagesBatchMasked into ndarray(l, m, k).

        Args:
        - nodules_df: dataframe of csv file with information
            about nodules location;
        - batch_size: number of nodules in the output batch. Must be int;
        - nodule_size: size of nodule along axes.
            Must be list, tuple or ndarray(3, ) of integer type;
            (Note: using zyx ordering)
        - share: share of cancer nodules in the batch.
            If source CTImagesBatch contains less cancer
            nodules than needed random nodules will be taken;
        - variance: variances of normally distributed random shifts of
            nodules' first pixels
        - mask_shape: needed shape of mask in (z, y, x)-order. If not None,
            masks of nodules will be scaled to shape=mask_shape
        - if_tensor: boolean flag. If set to True, return tuple (data, mask),
            where data and mask are 4d-tensors; first dim enumerates nodules,
            others are spatial
        """
        if self.nodules is None:
            raise AttributeError("Info about nodules location must " +
                                 "be loaded before calling this method")
        if variance is not None:
            variance = np.asarray(variance, dtype=np.int)
            variance = variance.flatten()
            if len(variance) != 3:
                logger.warning('Argument variance be np.array-like' +
                               'and has shape (3,). ' +
                               'Would be used no-scale-shift.')
                variance = None
        nodule_size = np.asarray(nodule_size, dtype=np.int)
        cancer_n = int(share * batch_size)
        cancer_n = self.num_nodules if cancer_n > self.num_nodules else cancer_n
        if self.num_nodules == 0:
            cancer_nodules = np.zeros((0, 3))
        else:
            sample_indices = np.random.choice(np.arange(self.num_nodules),
                                              size=cancer_n, replace=False)
            cancer_nodules = self._fit_into_bounds(nodule_size,
                                                   variance=variance)
            cancer_nodules = cancer_nodules[sample_indices, :]

        random_nodules = self.sample_random_nodules(batch_size - cancer_n,
                                                    nodule_size)

        nodules_indices = np.vstack([cancer_nodules,
                                     random_nodules]).astype(np.int)  # pylint: disable=no-member

        # crop nodules' data
        data = get_nodules_numba(self.data, nodules_indices, nodule_size)

        # if mask_shape not None, compute scaled mask for the whole batch
        # scale also nodules' starting positions and nodules' shapes
        if mask_shape is not None:
            scale_factor = np.asarray(mask_shape) / np.asarray(nodule_size)
            batch_mask_shape = np.rint(scale_factor * self.shape[0, :]).astype(np.int)
            batch_mask = self.fetch_mask_data(batch_mask_shape)
            nodules_indices = np.rint(scale_factor * nodules_indices).astype(np.int)
        else:
            batch_mask = self.mask
            mask_shape = nodule_size

        # crop nodules' masks
        mask = get_nodules_numba(batch_mask, nodules_indices, mask_shape)

        # if if_tensor, reshape nodules' data and mask to 4d-shape and return tuple
        if if_tensor:
            data = data.reshape((batch_size, ) + tuple(nodule_size))
            mask = mask.reshape((batch_size, ) + tuple(mask_shape))
            return data, mask

        bounds = np.arange(batch_size + 1) * nodule_size[0]

        nodules_batch = CTImagesMaskedBatch(self.make_indices(batch_size))
        nodules_batch.load(source=data, fmt='ndarray',
                           bounds=bounds, spacing=self.spacing)
        # TODO add info about nodules by changing self.nodules
        nodules_batch.mask = mask
        return nodules_batch

    def get_axial_slice(self, patient_pos, height):
        """Get tuple of slices (data slice, mask slice).

        Args:
            patient_pos: patient position in the batch
            height: height, take slices with number
                int(0.7 * number of slices for patient) from
                patient's scan and mask
        """
        margin = int(height * self[patient_pos].shape[0])
        if self.mask is not None:
            patch = (self.get_image(patient_pos)[margin, :, :],
                     self.get_mask(patient_pos)[margin, :, :])
        else:
            patch = (self.get_image(patient_pos)[margin, :, :], None)
        return patch

    def _refresh_nodules_info(self):
        """Refresh self.nodules attributes [spacing, origin, img_size, bias].

        This method should be called when it is needed to make
        [spacing, origin, img_size, bias] attributes of self.nodules
        to correspond the structure of batch's inner data.
        """
        self.nodules.offset[:, 0] = self.lower_bounds[self.nodules.patient_pos]
        self.nodules.spacing = self.spacing[self.nodules.patient_pos, :]
        self.nodules.origin = self.origin[self.nodules.patient_pos, :]
        self.nodules.img_size = self.shape[self.nodules.patient_pos, :]

    def _rescale_spacing(self):
        """Rescale spacing values and update nodules_info.

        This method should be called after any operation that
        changes shape of inner data.
        """
        if self.nodules is not None:
            self._refresh_nodules_info()
        return self

    @action
    @inbatch_parallel(init='_init_rebuild',
                      post='_post_rebuild', target='nogil')
    def resize(self, shape=(256, 256, 128), order=3, *args, **kwargs):    # pylint: disable=unused-argument, no-self-use
        """Perform resize of each CT-scan.

        performs resize (change of shape) of each CT-scan in the batch.
            When called from Batch, changes Batch
            returns self
        args:
            shape: needed shape after resize in order x, y, z
                *note that the order of axes in data is z, y, x
                 that is, new patient shape = (shape[2], shape[1], shape[0])
            n_workers: number of threads used (degree of parallelism)
                *note: available in the result of decoration of the function
                above
            order: the order of interpolation (<= 5)
                large value improves precision, but slows down the computaion
        example:
            shape = (256, 256, 128)
            Batch = Batch.resize(shape=shape, n_workers=20, order=2)
        """
        return resize_patient_numba

    def _post_rebuild(self, all_outputs, new_batch=False, **kwargs):
        """Post-function for resize parallelization.

        gatherer of outputs from different workers for
            ops, requiring complete rebuild of batch._data
        args:
            new_batch: if True, returns new batch with data
                agregated from workers_ouputs
        """
        # TODO: process errors
        batch = super()._post_rebuild(all_outputs, new_batch, **kwargs)
        batch.nodules = self.nodules
        batch._rescale_spacing()
        if self.mask is not None:
            batch.create_mask()
        return batch

    @action
    def make_xip(self, step=2, depth=10, func='max',
                 projection='axial', *args, **kwargs):    # pylint: disable=unused-argument, no-self-use
        """Compute xip of source CTImage along given x with given step and depth.

        Call parent variant of make_xip then change nodules sizes'
        via calling _update_nodule_size and create new mask that corresponds
        to data after transform.
        """
        batch = super().make_xip(step=step, depth=depth, func=func,
                                 projection=projection, *args, **kwargs)

        batch.nodules = self.nodules
        if projection == 'axial':
            pr = 0
        elif projection == 'coronal':
            pr = 1
        elif projection == 'sagital':
            pr = 2
        batch.nodules.nodule_size[:, pr] += depth * self.nodules.spacing[:, pr]
        batch.spacing = self.rescale(batch[0].shape)
        batch._rescale_spacing()
        if self.mask is not None:
            batch.create_mask()
        return batch


    def _update_nodule_size(self, step, depth, axis='z'):
        """Update nodules' sizes after xip operations.

        This function updates nodules when xip operation is performed
        is called after every xip operation.

        Args:
        - step: step of xip operation;
        - depth: depth of xip operation;
        - axis: axis along which xip operation is computed;
        """
        if axis == 'z':
            size_inc = np.array([depth, 0, 0])
        elif axis == 'y':
            size_inc = np.array([0, depth, 0])
        elif axis == 'x':
            size_inc = np.array([0, 0, depth])
        else:
            raise ValueError("Argument axis must be instance " +
                             "of type str and have one of the " +
                             "following values ['z', 'y', 'x']")
        self.nodules.nodule_size += size_inc * self.nodules.spacing

    def flip(self):
        logger.warning("There is no implementation of flip method for class " +
                       "CTIMagesMaskedBatch. Nothing happened")
        return self

    def visualize(self, index):
        """Visualize masked CTImage with ipyvolume.

        This method visualizes masked CTImages using ipyvolumne package.
        Points where mask has 1 values are supposed to be brighter in
        3d picture.

        Args:
        - index: int or str containing sample position
        or index correspondingly.
        """
        import ipyvolume
        data = self.get_image(index)
        mask = self.get_mask(index)
        return ipyvolume.quickvolshow(data + mask * 1000, level=[0.25, 0.75],
                                      opacity=0.03, level_width=0.1,
                                      data_min=0, data_max=1300)
