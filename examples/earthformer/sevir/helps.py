import paddle
from typing import Dict, Optional, Sequence
from paddle.nn import functional as F
from ppsci.data.dataset.sevir_dataset import SEVIRDataset
import numpy as np


def _threshold(target, pred, T):
    """
    Returns binary tensors t,p the same shape as target & pred.  t = 1 wherever
    target > t.  p =1 wherever pred > t.  p and t are set to 0 wherever EITHER
    t or p are nan.
    This is useful for counts that don't involve correct rejections.

    Parameters
    ----------
    target
        paddle.Tensor
    pred
        paddle.Tensor
    T
        numeric_type:   threshold
    Returns
    -------
    t
    p
    """
    t = (target >= T).astype('float32')
    p = (pred >= T).astype('float32')
    is_nan = paddle.logical_or(paddle.isnan(target),
                               paddle.isnan(pred))
    t[is_nan] = 0
    p[is_nan] = 0
    return t, p


class SEVIRSkillScore:
    r"""
    The calculation of skill scores in SEVIR challenge is slightly different:
        `mCSI = sum(mCSI_t) / T`
    See https://github.com/MIT-AI-Accelerator/sevir_challenges/blob/dev/radar_nowcasting/RadarNowcastBenchmarks.ipynb for more details.
    """
    full_state_update: bool = True

    def __init__(self,
                 layout: str = "NHWT",
                 mode: str = "0",
                 seq_len: Optional[int] = None,
                 preprocess_type: str = "sevir",
                 threshold_list: Sequence[int] = (16, 74, 133, 160, 181, 219),
                 metrics_list: Sequence[str] = ("csi", "bias", "sucr", "pod"),
                 eps: float = 1e-4,
                 dist_sync_on_step: bool = False,
                 ):
        """
        Parameters
        ----------
        seq_len
        layout
        mode:   str
            Should be in ("0", "1", "2")
            "0":
                cumulates hits/misses/fas of all test pixels
                score_avg takes average over all thresholds
                return
                    score_thresh shape = (1, )
                    score_avg shape = (1, )
            "1":
                cumulates hits/misses/fas of each step
                score_avg takes average over all thresholds while keeps the seq_len dim
                return
                    score_thresh shape = (seq_len, )
                    score_avg shape = (seq_len, )
            "2":
                cumulates hits/misses/fas of each step
                score_avg takes average over all thresholds, then takes average over the seq_len dim
                return
                    score_thresh shape = (1, )
                    score_avg shape = (1, )
        preprocess_type
        threshold_list
        """
        super().__init__()
        self.layout = layout
        self.preprocess_type = preprocess_type
        self.threshold_list = threshold_list
        self.metrics_list = metrics_list
        self.eps = eps
        self.mode = mode
        self.seq_len = seq_len

        self.hits = paddle.zeros(shape=[len(self.threshold_list)])
        self.misses = paddle.zeros(shape=[len(self.threshold_list)])
        self.fas = paddle.zeros(shape=[len(self.threshold_list)])

        if mode in ("0",):
            self.keep_seq_len_dim = False
            state_shape = (len(self.threshold_list),)
        elif mode in ("1", "2"):
            self.keep_seq_len_dim = True
            assert isinstance(self.seq_len, int), "seq_len must be provided when we need to keep seq_len dim."
            state_shape = (len(self.threshold_list), self.seq_len)

        else:
            raise NotImplementedError(f"mode {mode} not supported!")

    @staticmethod
    def pod(hits, misses, fas, eps):
        return hits / (hits + misses + eps)

    @staticmethod
    def sucr(hits, misses, fas, eps):
        return hits / (hits + fas + eps)

    @staticmethod
    def csi(hits, misses, fas, eps):
        return hits / (hits + misses + fas + eps)

    @staticmethod
    def bias(hits, misses, fas, eps):
        bias = (hits + fas) / (hits + misses + eps)
        logbias = paddle.pow(bias / paddle.log(paddle.to_tensor(2.0)), 2.0)
        return logbias

    @property
    def hits_misses_fas_reduce_dims(self):
        if not hasattr(self, "_hits_misses_fas_reduce_dims"):
            seq_dim = self.layout.find('T')
            self._hits_misses_fas_reduce_dims = list(range(len(self.layout)))
            if self.keep_seq_len_dim:
                self._hits_misses_fas_reduce_dims.pop(seq_dim)
        return self._hits_misses_fas_reduce_dims

    def calc_seq_hits_misses_fas(self, pred, target, threshold):
        """
        Parameters
        ----------
        pred, target:   torch.Tensor
        threshold:  int

        Returns
        -------
        hits, misses, fas:  torch.Tensor
            each has shape (seq_len, )
        """
        with paddle.no_grad():
            t, p = _threshold(target, pred, threshold)
            hits = paddle.sum(t * p, axis=self.hits_misses_fas_reduce_dims).astype('int32')
            misses = paddle.sum(t * (1 - p), axis=self.hits_misses_fas_reduce_dims).astype('int32')
            fas = paddle.sum((1 - t) * p, axis=self.hits_misses_fas_reduce_dims).astype('int32')
        return hits, misses, fas

    def preprocess(self, pred, target):
        if self.preprocess_type == "sevir":
            pred = SEVIRDataset.process_data_dict_back(
                data_dict={'vil': pred.detach().astype('float32')})['vil']
            target = SEVIRDataset.process_data_dict_back(
                data_dict={'vil': target.detach().astype('float32')})['vil']
        else:
            raise NotImplementedError
        return pred, target

    def compute(self, pred: paddle.Tensor, target: paddle.Tensor):
        pred, target = self.preprocess(pred, target)
        for i, threshold in enumerate(self.threshold_list):
            hits, misses, fas = self.calc_seq_hits_misses_fas(pred, target, threshold)
            self.hits[i] += hits
            self.misses[i] += misses
            self.fas[i] += fas

        metrics_dict = {
            'pod': self.pod,
            'csi': self.csi,
            'sucr': self.sucr,
            'bias': self.bias}
        ret = {}
        for threshold in self.threshold_list:
            ret[threshold] = {}
        ret["avg"] = {}
        for metrics in self.metrics_list:
            if self.keep_seq_len_dim:
                score_avg = np.zeros((self.seq_len,))
            else:
                score_avg = 0
            # shape = (len(threshold_list), seq_len) if self.keep_seq_len_dim,
            # else shape = (len(threshold_list),)
            scores = metrics_dict[metrics](self.hits, self.misses, self.fas, self.eps)
            scores = scores.detach().cpu().numpy()
            for i, threshold in enumerate(self.threshold_list):
                if self.keep_seq_len_dim:
                    score = scores[i]  # shape = (seq_len, )
                else:
                    score = scores[i].item()  # shape = (1, )
                if self.mode in ("0", "1"):
                    ret[threshold][metrics] = score
                elif self.mode in ("2",):
                    ret[threshold][metrics] = np.mean(score).item()
                else:
                    raise NotImplementedError
                score_avg += score
            score_avg /= len(self.threshold_list)
            if self.mode in ("0", "1"):
                ret["avg"][metrics] = score_avg
            elif self.mode in ("2",):
                ret["avg"][metrics] = np.mean(score_avg).item()
            else:
                raise NotImplementedError
        return ret


class eval_rmse_func:
    def __init__(self,
                 out_len=12,
                 layout='NTHWC',
                 metrics_mode='0',
                 metrics_list=['csi', 'pod', 'sucr', 'bias'],
                 threshold_list=[16, 74, 133, 160, 181, 219],
                 *args,
                 ) -> Dict[str, paddle.Tensor]:
        super().__init__()
        self.out_len = out_len
        self.layout = layout
        self.metrics_mode = metrics_mode
        self.metrics_list = metrics_list
        self.threshold_list = threshold_list

    def __call__(self,
                 output_dict: Dict[str, "paddle.Tensor"],
                 label_dict: Dict[str, "paddle.Tensor"]
                 ):
        pred = output_dict["vil"]
        vil_target = label_dict["vil"]
        vil_target = vil_target.reshape([-1, *vil_target.shape[2:]])
        # mse
        mae = F.l1_loss(pred, vil_target, "none")
        mae = mae.mean(axis=tuple(range(1, mae.ndim)))
        # mse
        mse = F.mse_loss(pred, vil_target, "none")
        mse = mse.mean(axis=tuple(range(1, mse.ndim)))

        sevir_score = SEVIRSkillScore(
            layout=self.layout,
            mode=self.metrics_mode,
            seq_len=self.out_len,
            threshold_list=self.threshold_list,
            metrics_list=self.metrics_list,
        )

        B = pred.shape[0]
        csi_m = paddle.zeros(shape=[B])
        csi_219 = paddle.zeros(shape=[B])
        csi_181 = paddle.zeros(shape=[B])
        csi_160 = paddle.zeros(shape=[B])
        csi_133 = paddle.zeros(shape=[B])
        csi_74 = paddle.zeros(shape=[B])
        csi_16 = paddle.zeros(shape=[B])
        csi_loss = paddle.zeros(shape=[B])
        for i in range(B):
            sevir_valid_score = sevir_score.compute(pred[i, ...].unsqueeze(0), vil_target[i, ...].unsqueeze(0))
            csi_loss[i] = - paddle.to_tensor(sevir_valid_score['avg']['csi'])
            csi_m[i] = paddle.to_tensor(sevir_valid_score['avg']['csi'])
            csi_219[i] = paddle.to_tensor(sevir_valid_score[219]['csi'])
            csi_181[i] = paddle.to_tensor(sevir_valid_score[181]['csi'])
            csi_160[i] = paddle.to_tensor(sevir_valid_score[160]['csi'])
            csi_133[i] = paddle.to_tensor(sevir_valid_score[133]['csi'])
            csi_74[i] = paddle.to_tensor(sevir_valid_score[74]['csi'])
            csi_16[i] = paddle.to_tensor(sevir_valid_score[16]['csi'])

        return {"valid_loss_epoch": csi_loss, "mse": mse, "mae": mae, "csi_m": csi_m,
                "csi_219": csi_219, "csi_181": csi_181, "csi_160": csi_160, "csi_133": csi_133,
                "csi_74": csi_74, "csi_16": csi_16}


def train_mse_func(
        output_dict: Dict[str, "paddle.Tensor"],
        label_dict: Dict[str, "paddle.Tensor"],
        *args,
) -> paddle.Tensor:
    pred = output_dict["vil"]
    vil_target = label_dict["vil"]
    target = vil_target.reshape([-1, *vil_target.shape[2:]])
    return F.mse_loss(pred, target)


def get_parameter_names(model, forbidden_layer_types):
    result = []
    for name, child in model.named_children():
        result += [
            f"{name}.{n}"
            for n in get_parameter_names(child, forbidden_layer_types)
            if not isinstance(child, tuple(forbidden_layer_types))
        ]
    # Add model specific parameters (defined with nn.Parameter) since they are not in any child.
    result += list(model._parameters.keys())
    return result
