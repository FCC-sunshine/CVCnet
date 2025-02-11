import torch
from torch import nn
from torch.nn import functional as F




class PSPModule(nn.Module):
    # (1, 2, 3, 6)
    def __init__(self, sizes=(1, 3, 6, 8), dimension=2):
        super(PSPModule, self).__init__()
        # self.stages = nn.ModuleList([self._make_stage(size, dimension) for size in sizes])
        self.stages1 = nn.AvgPool2d(3)
        self.stages3 = nn.AvgPool2d(6)
        self.stages6 = nn.AvgPool2d(9)
        self.stages8 = nn.AvgPool2d(12)
        

    def _make_stage(self, size, dimension=2):
        if dimension == 1:
            prior = nn.AdaptiveAvgPool1d(output_size=size)
        elif dimension == 2:
            prior = nn.AdaptiveAvgPool2d(output_size=(size, size))
        elif dimension == 3:
            prior = nn.AdaptiveAvgPool3d(output_size=(size, size, size))
        return prior

    def forward(self, feats):
        n, c, _, _ = feats.size()
        # priors = [stage(feats).view(n, c, -1) for stage in self.stages]
        priors1=self.stages1(feats).view(n, c, -1)
        priors3 = self.stages3(feats).view(n, c, -1)
        priors6 = self.stages6(feats).view(n, c, -1)
        priors8 = self.stages8(feats).view(n, c, -1)
        
        center = torch.cat((priors1,priors3,priors6,priors8), -1)
        return center


class _SelfAttentionBlock(nn.Module):
    '''
    The basic implementation for self-attention block/non-local block
    Input:
        N X C X H X W
    Parameters:
        in_channels       : the dimension of the input feature map
        key_channels      : the dimension after the key/query transform
        value_channels    : the dimension after the value transform
        scale             : choose the scale to downsample the input feature maps (save memory cost)
    Return:
        N X C X H X W
        position-aware context features.(w/o concate or add with the input)
    '''
    def __init__(self, low_in_channels, high_in_channels, key_channels, value_channels, out_channels=None, scale=1, norm_type=None,psp_size=(1,3,6,8)):
        super(_SelfAttentionBlock, self).__init__()
        self.scale = scale
        self.in_channels = low_in_channels
        self.out_channels = out_channels
        self.key_channels = key_channels
        self.value_channels = value_channels
        if out_channels == None:
            self.out_channels = high_in_channels
        self.pool = nn.MaxPool2d(kernel_size=(scale, scale))
        self.f_key = nn.Sequential(
            nn.Conv2d(in_channels=self.in_channels, out_channels=self.key_channels,
                      kernel_size=1, stride=1, padding=0)
            # ModuleHelper.BNReLU(self.key_channels, norm_type=norm_type),
        )
        self.f_query = nn.Sequential(
            nn.Conv2d(in_channels=high_in_channels, out_channels=self.key_channels,
                      kernel_size=1, stride=1, padding=0),
            # ModuleHelper.BNReLU(self.key_channels, norm_type=norm_type),
        )
        self.f_value = nn.Conv2d(in_channels=self.in_channels, out_channels=self.value_channels,
                                 kernel_size=1, stride=1, padding=0)
        self.W = nn.Conv2d(in_channels=self.value_channels, out_channels=self.out_channels,
                           kernel_size=1, stride=1, padding=0)

        self.psp = PSPModule(psp_size)
        # nn.init.constant_(self.W.weight, 0)
        # nn.init.constant_(self.W.bias, 0)

    def forward(self, low_feats, high_feats):
        batch_size, h, w = high_feats.size(0), high_feats.size(2), high_feats.size(3)
        # if self.scale > 1:
        #     x = self.pool(x)
        value = self.psp(self.f_value(low_feats))#这里就是进行pooling操作的方法

        query = self.f_query(high_feats).view(batch_size, self.key_channels, -1)
        query = query.permute(0, 2, 1)
        key = self.f_key(low_feats)
        # value=self.psp(value)#.view(batch_size, self.value_channels, -1)
        value = value.permute(0, 2, 1)
        key = self.psp(key)  # .view(batch_size, self.key_channels, -1)
        sim_map = torch.matmul(query, key)
        # sim_map = (self.key_channels ** -.5) * sim_map
        sim_map = F.softmax(sim_map, dim=-1)
        # sim_map=(sim_map>0.01)+0
        # print("sim_map ",sim_map)
        # print("sim_map>0.5",(sim_map>0.01).float())
        # print("sum",torch.sum((sim_map>0.01)+0),1)
        # zzz=torch.sum(sim_map)
        # print("size",sim_map.size())
        # R_lv3_star, R_lv3_star_arg = torch.max(sim_map, dim=1)
        # print("R_lv3_star",R_lv3_star)
        # print("R_lv3_star_arg", R_lv3_star_arg)

        sim_map_temp=(sim_map>0.009).float()
        sim_map=sim_map*sim_map_temp

        context = torch.matmul(sim_map, value)
        # print("context",context)
        # print("context>0.5",torch.matmul((sim_map>0.5), value))
        context = context.permute(0, 2, 1).contiguous()
        context = context.view(batch_size, self.value_channels, *high_feats.size()[2:])
        context = self.W(context)
        return context


class SelfAttentionBlock2D(_SelfAttentionBlock):
    def __init__(self, low_in_channels, high_in_channels, key_channels, value_channels, out_channels=None, scale=1, norm_type=None,psp_size=(1,3,6,8)):
        super(SelfAttentionBlock2D, self).__init__(low_in_channels,
                                                   high_in_channels,
                                                   key_channels,
                                                   value_channels,
                                                   out_channels,
                                                   scale,
                                                   norm_type,
                                                   psp_size=psp_size
                                                   )


class CVA(nn.Module):
    """
    Parameters:
        in_features / out_features: the channels of the input / output feature maps.
        dropout: we choose 0.05 as the default value.
        size: you can apply multiple sizes. Here we only use one size.
    Return:
        features fused with Object context information.
    """

    def __init__(self, low_in_channels, high_in_channels, out_channels, key_channels, value_channels, dropout,
                 sizes=([1]), norm_type=None,psp_size=(1,3,6,8)):
        super(CVA, self).__init__()
        self.stages = []
        self.norm_type = norm_type
        self.psp_size=psp_size
        self.stages = nn.ModuleList(
            [self._make_stage([low_in_channels, high_in_channels], out_channels, key_channels, value_channels, size) for
             size in sizes])

    def _make_stage(self, in_channels, output_channels, key_channels, value_channels, size):
        return SelfAttentionBlock2D(in_channels[0],
                                    in_channels[1],
                                    key_channels,
                                    value_channels,
                                    output_channels,
                                    size,
                                    self.norm_type,
                                    psp_size=self.psp_size)

    def forward(self, low_feats, high_feats):

        priors = [stage(low_feats, high_feats) for stage in self.stages]
        context = priors[0]
        return context

