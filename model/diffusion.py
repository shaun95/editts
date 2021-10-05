# Modified from original Grad-TTS code: https://github.com/huawei-noah/Speech-Backbones/tree/main/Grad-TTS

import math
import torch
from einops import rearrange

from model.base import BaseModule
from model.commons import shift_mel
import torch.nn.functional as F

class Mish(BaseModule):
    def forward(self, x):
        return x * torch.tanh(torch.nn.functional.softplus(x))


class Upsample(BaseModule):
    def __init__(self, dim):
        super(Upsample, self).__init__()
        self.conv = torch.nn.ConvTranspose2d(dim, dim, 4, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Downsample(BaseModule):
    def __init__(self, dim):
        super(Downsample, self).__init__()
        self.conv = torch.nn.Conv2d(dim, dim, 3, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Rezero(BaseModule):
    def __init__(self, fn):
        super(Rezero, self).__init__()
        self.fn = fn
        self.g = torch.nn.Parameter(torch.zeros(1))

    def forward(self, x):
        return self.fn(x) * self.g


class Block(BaseModule):
    def __init__(self, dim, dim_out, groups=8):
        super(Block, self).__init__()
        self.block = torch.nn.Sequential(
            torch.nn.Conv2d(dim, dim_out, 3, padding=1),
            torch.nn.GroupNorm(groups, dim_out),
            Mish(),
        )

    def forward(self, x, mask):
        output = self.block(x * mask)
        return output * mask


class ResnetBlock(BaseModule):
    def __init__(self, dim, dim_out, time_emb_dim, groups=8):
        super(ResnetBlock, self).__init__()
        self.mlp = torch.nn.Sequential(Mish(), torch.nn.Linear(time_emb_dim, dim_out))

        self.block1 = Block(dim, dim_out, groups=groups)
        self.block2 = Block(dim_out, dim_out, groups=groups)
        if dim != dim_out:
            self.res_conv = torch.nn.Conv2d(dim, dim_out, 1)
        else:
            self.res_conv = torch.nn.Identity()

    def forward(self, x, mask, time_emb):
        h = self.block1(x, mask)
        h += self.mlp(time_emb).unsqueeze(-1).unsqueeze(-1)
        h = self.block2(h, mask)
        output = h + self.res_conv(x * mask)
        return output


class LinearAttention(BaseModule):
    def __init__(self, dim, heads=4, dim_head=32):
        super(LinearAttention, self).__init__()
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_qkv = torch.nn.Conv2d(dim, hidden_dim * 3, 1, bias=False)
        self.to_out = torch.nn.Conv2d(hidden_dim, dim, 1)

    def forward(self, x):
        b, c, h, w = x.shape
        qkv = self.to_qkv(x)
        q, k, v = rearrange(
            qkv, "b (qkv heads c) h w -> qkv b heads c (h w)", heads=self.heads, qkv=3
        )
        k = k.softmax(dim=-1)
        context = torch.einsum("bhdn,bhen->bhde", k, v)
        out = torch.einsum("bhde,bhdn->bhen", context, q)
        out = rearrange(
            out, "b heads c (h w) -> b (heads c) h w", heads=self.heads, h=h, w=w
        )
        return self.to_out(out)


class Residual(BaseModule):
    def __init__(self, fn):
        super(Residual, self).__init__()
        self.fn = fn

    def forward(self, x, *args, **kwargs):
        output = self.fn(x, *args, **kwargs) + x
        return output


class SinusoidalPosEmb(BaseModule):
    def __init__(self, dim):
        super(SinusoidalPosEmb, self).__init__()
        self.dim = dim

    def forward(self, x, scale=1000):
        device = x.device
        half_dim = self.dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device).float() * -emb)
        emb = scale * x.unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb


class GradLogPEstimator2d(BaseModule):
    def __init__(self, dim, dim_mults=(1, 2, 4), groups=8, pe_scale=1000):
        super(GradLogPEstimator2d, self).__init__()
        self.pe_scale = pe_scale

        dims = [2, *map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))

        self.time_pos_emb = SinusoidalPosEmb(dim)
        self.mlp = torch.nn.Sequential(
            torch.nn.Linear(dim, dim * 4), Mish(), torch.nn.Linear(dim * 4, dim)
        )

        self.downs = torch.nn.ModuleList([])
        self.ups = torch.nn.ModuleList([])
        num_resolutions = len(in_out)

        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)
            self.downs.append(
                torch.nn.ModuleList(
                    [
                        ResnetBlock(dim_in, dim_out, time_emb_dim=dim),
                        ResnetBlock(dim_out, dim_out, time_emb_dim=dim),
                        Residual(Rezero(LinearAttention(dim_out))),
                        Downsample(dim_out) if not is_last else torch.nn.Identity(),
                    ]
                )
            )

        mid_dim = dims[-1]
        self.mid_block1 = ResnetBlock(mid_dim, mid_dim, time_emb_dim=dim)
        self.mid_attn = Residual(Rezero(LinearAttention(mid_dim)))
        self.mid_block2 = ResnetBlock(mid_dim, mid_dim, time_emb_dim=dim)

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out[1:])):
            self.ups.append(
                torch.nn.ModuleList(
                    [
                        ResnetBlock(dim_out * 2, dim_in, time_emb_dim=dim),
                        ResnetBlock(dim_in, dim_in, time_emb_dim=dim),
                        Residual(Rezero(LinearAttention(dim_in))),
                        Upsample(dim_in),
                    ]
                )
            )
        self.final_block = Block(dim, dim)
        self.final_conv = torch.nn.Conv2d(dim, 1, 1)

    def forward(self, x, mask, mu, t):
        t = self.time_pos_emb(t, scale=self.pe_scale)
        t = self.mlp(t)

        x = torch.stack([mu, x], 1)
        mask = mask.unsqueeze(1)

        hiddens = []
        masks = [mask]
        for resnet1, resnet2, attn, downsample in self.downs:
            mask_down = masks[-1]
            x = resnet1(x, mask_down, t)
            x = resnet2(x, mask_down, t)
            x = attn(x)
            hiddens.append(x)
            x = downsample(x * mask_down)
            masks.append(mask_down[:, :, :, ::2])

        masks = masks[:-1]
        mask_mid = masks[-1]
        x = self.mid_block1(x, mask_mid, t)
        x = self.mid_attn(x)
        x = self.mid_block2(x, mask_mid, t)

        for resnet1, resnet2, attn, upsample in self.ups:
            mask_up = masks.pop()
            x = torch.cat((x, hiddens.pop()), dim=1)
            x = resnet1(x, mask_up, t)
            x = resnet2(x, mask_up, t)
            x = attn(x)
            x = upsample(x * mask_up)

        x = self.final_block(x, mask)
        output = self.final_conv(x * mask)

        return (output * mask).squeeze(1)


def get_noise(t, beta_init, beta_term, cumulative=False):
    if cumulative:
        noise = beta_init * t + 0.5 * (beta_term - beta_init) * (t ** 2)
    else:
        noise = beta_init + (beta_term - beta_init) * t
    return noise


class Diffusion(BaseModule):
    def __init__(self, n_feats, dim, beta_min, beta_max, pe_scale):
        super(Diffusion, self).__init__()
        self.estimator = GradLogPEstimator2d(dim, pe_scale=pe_scale)
        self.n_feats = n_feats
        self.dim = dim
        self.beta_min = beta_min
        self.beta_max = beta_max
        self.pe_scale = pe_scale

    def forward_diffusion(self, x0, mask, mu, t):
        time = t.unsqueeze(-1).unsqueeze(-1)
        cum_noise = get_noise(time, self.beta_min, self.beta_max, cumulative=True)
        mean = x0 * torch.exp(-0.5 * cum_noise) + mu * (
            1.0 - torch.exp(-0.5 * cum_noise)
        )
        variance = 1.0 - torch.exp(-cum_noise)
        z = torch.randn(x0.shape, dtype=x0.dtype, device=x0.device, requires_grad=False)
        xt = mean + z * torch.sqrt(variance)
        return xt * mask, z * mask

    @torch.no_grad()
    def reverse_diffusion(self, z, mask, mu, n_timesteps, stoc=False):
        h = 1.0 / n_timesteps
        xt = z * mask
        for i in range(n_timesteps):
            t = (1.0 - (i + 0.5) * h) * torch.ones(
                z.shape[0], dtype=z.dtype, device=z.device
            )
            time = t.unsqueeze(-1).unsqueeze(-1)
            noise_t = get_noise(time, self.beta_min, self.beta_max, cumulative=False)
            if stoc:  # adds stochastic term
                dxt_det = 0.5 * (mu - xt) - self.estimator(xt, mask, mu, t)
                dxt_det = dxt_det * noise_t * h
                dxt_stoc = torch.randn(
                    z.shape, dtype=z.dtype, device=z.device, requires_grad=False
                )
                dxt_stoc = dxt_stoc * torch.sqrt(noise_t * h)
                dxt = dxt_det + dxt_stoc
            else:
                dxt = 0.5 * (mu - xt - self.estimator(xt, mask, mu, t))
                dxt = dxt * noise_t * h
            xt = (xt - dxt) * mask
        return xt

    @torch.no_grad()
    def forward(self, z, mask, mu, n_timesteps, stoc=False):
        return self.reverse_diffusion(z, mask, mu, n_timesteps, stoc)

    def loss_t(self, x0, mask, mu, t):
        xt, z = self.forward_diffusion(x0, mask, mu, t)
        time = t.unsqueeze(-1).unsqueeze(-1)
        cum_noise = get_noise(time, self.beta_min, self.beta_max, cumulative=True)
        noise_estimation = self.estimator(xt, mask, mu, t)
        noise_estimation *= torch.sqrt(1.0 - torch.exp(-cum_noise))
        loss = torch.sum((noise_estimation + z) ** 2) / (torch.sum(mask) * self.n_feats)
        return loss

    def compute_loss(self, x0, mask, mu, offset=1e-5):
        t = torch.rand(
            x0.shape[0], dtype=x0.dtype, device=x0.device, requires_grad=False
        )
        t = torch.clamp(t, offset, 1.0 - offset)
        return self.loss_t(x0, mask, mu, t)

    @torch.no_grad()
    def double_forward_pitch(
        self, z, z_edit, mu, mu_edit, mask, mask_edit, n_timesteps, stoc=False, soften_mask=True, n_soften=20
    ):
        if soften_mask:
            kernel = [2 ** ((n_soften-1)-abs(n_soften-1-i)) for i in range(2 * n_soften - 1)] # [1, 2, 4, ..., 2^n_soften , 2^(n_soften-1), ..., 2, 1]
            kernel = [i/sum(kernel[:len(kernel)//2+1]) for i in kernel]
            w = torch.tensor(kernel).view(1, 1, 1, len(kernel)).to(mask_edit.device)
            mask_edit_soft = mask_edit.unsqueeze(1).contiguous()
            mask_edit_soft = F.pad(mask_edit_soft, (len(kernel)//2, len(kernel)//2, 0, 0), mode="replicate")
            mask_edit_soft = F.conv2d(
                mask_edit_soft,
                w,
                bias=None,
                stride=1,
            )
            mask_edit_soft = mask_edit_soft.squeeze(1)
            mask_edit = mask_edit + (1 - mask_edit) * mask_edit_soft

        h = 1.0 / n_timesteps
        xt = z * mask
        xt_edit = z_edit * mask

        for i in range(n_timesteps): 
            t = (1.0 - (i + 0.5) * h) * torch.ones(
                z.shape[0], dtype=z.dtype, device=z.device
            )
            time = t.unsqueeze(-1).unsqueeze(-1)
            noise_t = get_noise(time, self.beta_min, self.beta_max, cumulative=False)
            if stoc:  # adds stochastic term
                # NOTE: should not come here
                assert False
                dxt_det = 0.5 * (mu - xt) - self.estimator(xt, mask, mu, t)
                dxt_det = dxt_det * noise_t * h
                dxt_stoc = torch.randn(
                    z.shape, dtype=z.dtype, device=z.device, requires_grad=False
                )
                dxt_stoc = dxt_stoc * torch.sqrt(noise_t * h)
                dxt = dxt_det + dxt_stoc
            else:
                dxt = 0.5 * (mu - xt - self.estimator(xt, mask, mu, t))
                dxt = dxt * noise_t * h
                dxt_edit = 0.5 * (
                    mu_edit - xt_edit - self.estimator(xt_edit, mask, mu_edit, t)
                )
                dxt_edit = dxt_edit * noise_t * h
            xt = (xt - dxt) * mask
            xt_edit = (
                xt_edit - ((1-mask_edit) * dxt + mask_edit * dxt_edit)
            ) * mask
        return xt, xt_edit

    @torch.no_grad()
    def double_forward_text(
        self, z, z_edit, mu, mu_edit, mask, mask_edit_net, mask_edit_grad, i1, j1, i2, j2, n_timesteps, stoc=False, soften_mask=True, n_soften=20
    ):
        if soften_mask:
            kernel = [2 ** ((n_soften-1)-abs(n_soften-1-i)) for i in range(2 * n_soften - 1)] # [1, 2, 4, ..., 2^n_soften , 2^(n_soften-1), ..., 2, 1]
            kernel = [i/sum(kernel[:len(kernel)//2+1]) for i in kernel]
            w = torch.tensor(kernel).view(1, 1, 1, len(kernel)).to(mask_edit_grad.device).float()
            mask_edit_soft = mask_edit_grad.unsqueeze(1).contiguous()
            mask_edit_soft = F.pad(mask_edit_soft, (len(kernel)//2, len(kernel)//2, 0, 0), mode="replicate")
            mask_edit_soft = F.conv2d(
                mask_edit_soft,
                w,
                bias=None,
                stride=1,
            )
            mask_edit_soft = mask_edit_soft.squeeze(1)
            mask_edit_grad = mask_edit_grad + (1 - mask_edit_grad) * mask_edit_soft
            
        h = 1.0 / n_timesteps
        xt = z * mask
        xt_edit = z_edit * mask_edit_net

        for i in range(n_timesteps): 
            t = (1.0 - (i + 0.5) * h) * torch.ones(
                z.shape[0], dtype=z.dtype, device=z.device
            )
            time = t.unsqueeze(-1).unsqueeze(-1)
            noise_t = get_noise(time, self.beta_min, self.beta_max, cumulative=False)
            if stoc:  # adds stochastic term
                # NOTE: should not come here
                assert False
                dxt_det = 0.5 * (mu - xt) - self.estimator(xt, mask, mu, t)
                dxt_det = dxt_det * noise_t * h
                dxt_stoc = torch.randn(
                    z.shape, dtype=z.dtype, device=z.device, requires_grad=False
                )
                dxt_stoc = dxt_stoc * torch.sqrt(noise_t * h)
                dxt = dxt_det + dxt_stoc
            else:
                dxt = 0.5 * (mu - xt - self.estimator(xt, mask, mu, t))
                dxt = dxt * noise_t * h
                dxt_edit = 0.5 * (
                    mu_edit - xt_edit - self.estimator(xt_edit, mask_edit_net, mu_edit, t)
                )
                dxt_edit = dxt_edit * noise_t * h
                
            xt = (xt - dxt) * mask

            dxt_trg = torch.zeros_like(dxt_edit)
            dxt_trg[:, :, i1:i1+(j2-i2)] = dxt[:, :, i2:j2]

            xt_edit = (
                xt_edit - (mask_edit_grad * dxt_trg + (1-mask_edit_grad) * dxt_edit)
            ) * mask_edit_net

        return xt, xt_edit