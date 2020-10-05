import torch
from torch import nn
from torch.distributions import Categorical
from torch.nn import functional as F
import math
from constants import *

def get_entropy(c_ij):
        c_ij = c_ij.permute(0,3,4,1,2).contiguous()
        entropy = Categorical(probs=c_ij).entropy()
        entropy = entropy/math.log(c_ij.size(4))
        entropy = entropy.permute(0,3,1,2)
        return entropy

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(self.expansion*planes)
            )

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = F.relu(out)
        return out

class ResNetPreCapsule(nn.Module):
    def __init__(self, block, num_blocks):
        super(ResNetPreCapsule, self).__init__()
        self.in_planes = 64

        self.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)#(b_size,64,32,32)
        self.bn1 = nn.BatchNorm2d(64)
        self.layer1 = self._make_layer(block, 64, num_blocks[0], stride=1)#(b_size,64,32,32)
        self.layer2 = self._make_layer(block, 128, num_blocks[1], stride=2)#(b_size,128,16,16)

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1]*(num_blocks-1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.layer1(out)
        out = self.layer2(out)
        return out

class PrimaryCapsules(nn.Module):
    def __init__(self,in_channels,num_capsules,out_dim,H=16,W=16):
        super(PrimaryCapsules,self).__init__()
        self.in_channels = in_channels
        self.num_capsules = num_capsules
        self.out_dim = out_dim
        self.H = H
        self.W = W
        self.capsule_preds = nn.Sequential(
                                   nn.Conv2d(in_channels,num_capsules*out_dim,kernel_size=1),
                                   nn.LayerNorm((num_capsules*out_dim,H,W)))
        self.activ_preds = nn.Sequential(
                                   nn.Conv2d(in_channels,num_capsules,kernel_size=1),
                                   nn.LayerNorm((num_capsules,H,W)))

    def forward(self,x):
        primary_capsules = self.capsule_preds(x)
        primary_activations = self.activ_preds(x)
        primary_capsules = primary_capsules.view(-1,self.num_capsules,self.out_dim,self.H,self.W)
        primary_activations = primary_activations.view(-1,self.num_capsules,1,self.H,self.W)
        return primary_capsules, primary_activations

class ConvCapsule(nn.Module):
    def __init__(self,in_caps,in_dim,out_caps,out_dim,kernel_size,stride,padding,analysis):
        super(ConvCapsule,self).__init__()
        self.in_caps = in_caps
        self.in_dim = in_dim
        self.analysis = analysis
        self.out_caps = out_caps
        self.out_dim = out_dim
        self.preds = nn.Sequential(
                                   nn.Conv2d(in_dim,out_caps*out_dim,kernel_size=kernel_size,stride=stride,padding=padding),
                                   nn.BatchNorm2d(out_caps*out_dim))
        self.pool = nn.AvgPool2d(kernel_size,stride,padding)
     
    def forward(self,in_capsules,in_capsule_activations,ITER=3):
        batch_size, _, _, H, W = in_capsules.size()
        in_capsules = in_capsules.view(batch_size*self.in_caps,self.in_dim,H,W)
        predictions = self.preds(in_capsules)
        _,_, H, W = predictions.size()
        predictions = predictions.view(batch_size, self.in_caps, self.out_caps*self.out_dim, H, W)
        predictions = predictions.view(batch_size, self.in_caps, self.out_caps, self.out_dim, H, W)
        in_capsule_activations = self.pool(in_capsule_activations.squeeze(2)).unsqueeze(2)
        if self.analysis == False:
           out_capsules, activations, entropy = self.clustering_routing(predictions,in_capsule_activations,ITER)
           return out_capsules, activations, entropy
        else:
             out_capsules, entropy = self.clustering_routing(predictions,in_capsule_activations,ITER)
             return out_capsules, entropy

    def squash(self, inputs, dim):
        norm = torch.norm(inputs, p=2, dim=dim, keepdim=True)
        scale = norm**2 / (1 + norm**2) / (norm + 1e-8)
        return scale * inputs

    def clustering_routing(self,predictions,in_capsule_activations,ITER=3):
        batch_size,_,_,_, H, W = predictions.size()
        b_ij = torch.zeros(batch_size,self.in_caps,self.out_caps,1,H,W).to(DEVICE)
        
        for it in range(ITER):
            c_ij = F.softmax(b_ij, dim=2)
            s_j = (c_ij * predictions * in_capsule_activations.unsqueeze(2)).sum(dim=1, keepdim=True)
            s_j = s_j/((c_ij*in_capsule_activations.unsqueeze(2)).sum(dim=1, keepdim=True)+1e-8)
            if it < ITER - 1: 
               delta = (predictions - s_j).norm(dim=3, keepdim=True)**2
               b_ij = b_ij - delta*in_capsule_activations.unsqueeze(2)
        c_ij = c_ij.squeeze(3)
        v_j = self.squash(s_j,3)
        activations = F.softmax((c_ij*in_capsule_activations,dim=2).sum(dim=1,keepdim=True))
        entropy = get_entropy(c_ij)
        return v_j.squeeze(dim=1), activations.squeeze(dim=1), entropy.mean()

    def dynamic_routing(self,predictions,ITER=3):
        batch_size,_,_,_, H, W = predictions.size()
        b_ij = torch.zeros(batch_size,self.in_caps,self.out_caps,1,H,W).to(DEVICE)
        for it in range(ITER):
            c_ij = F.softmax(b_ij, dim=2)
            s_j = (c_ij * predictions).sum(dim=1, keepdim=True)
            v_j = self.squash(inputs=s_j, dim=3)
            if it < ITER - 1: 
               delta = (predictions * v_j).sum(dim=3, keepdim=True)
               b_ij = b_ij + delta
        c_ij = c_ij.squeeze(3)
        entropy = get_entropy(c_ij)
        return v_j.squeeze(dim=1), entropy.mean()

class ResnetCnnsovnetDynamicRouting(nn.Module):
    def __init__(self,analysis=False):
        super(ResnetCnnsovnetDynamicRouting,self).__init__()
        self.resnet_precaps = ResNetPreCapsule(BasicBlock,[3,4]) 
        self.primary_caps = PrimaryCapsules(128,32,16,16,16)#for cifar10, H,W = 16, 16. For MNIST etc. H,W = 14,14.
        self.conv_caps1 = ConvCapsule(in_caps=32,in_dim=16,out_caps=32,out_dim=16,kernel_size=3,stride=2,padding=0,analysis=analysis)
        self.conv_caps2 = ConvCapsule(in_caps=32,in_dim=16,out_caps=32,out_dim=16,kernel_size=3,stride=1,padding=0,analysis=analysis)
        self.conv_caps3 = ConvCapsule(in_caps=32,in_dim=16,out_caps=32,out_dim=16,kernel_size=3,stride=1,padding=0,analysis=analysis)
        #self.conv_caps4 = ConvCapsule(in_caps=32,in_dim=16,out_caps=32,out_dim=16,kernel_size=3,stride=1,padding=1,analysis=analysis) 
        self.class_caps = ConvCapsule(in_caps=32,in_dim=16,out_caps=10,out_dim=16,kernel_size=3,stride=1,padding=0,analysis=analysis)
        self.linear = nn.Linear(16,1)
        self.analysis = analysis
        

    def forward(self,x):
        conv_output = self.resnet_precaps(x)
        primary_caps, primary_activ = self.primary_caps(conv_output)
        if self.analysis == False:
           #capsules are returned, entropies are summed
           conv_caps1, caps1_activation, entropy = self.conv_caps1(primary_caps,primary_activ)
           #mean of entropy is taken on H,W,capsules
           entropy = entropy.mean()
           #print(entropy.size()) 
           conv_caps2, caps2_activation ,temp = self.conv_caps2(conv_caps1,caps1_activation)
           entropy += temp.mean()
           #print(entropy.size())
           conv_caps3, caps3_activation ,temp = self.conv_caps3(conv_caps2,caps2_activation)
           entropy += temp.mean()
           #print(entropy.size())
           #conv_caps4, temp = self.conv_caps4(conv_caps3)
           #entropy += temp.mean()
           #print(entropy.size())
           class_caps, class_prediction ,temp = self.class_caps(conv_caps3,caps3_activation)
           entropy += temp.mean()
           #print(entropy.size())
        else:
             #entropies are returned
             conv_caps1, cij_entropy1 = self.conv_caps1(primary_caps)
             conv_caps2, cij_entropy2 = self.conv_caps2(conv_caps1)
             conv_caps3, cij_entropy3 = self.conv_caps3(conv_caps2)
             class_caps, cij_entropy4 = self.class_caps(conv_caps3)
             entropies = [cij_entropy1,cij_entropy2,cij_entropy3,cij_entropy4]            
        #class_caps = class_caps.squeeze()
        #class_predictions = self.linear(class_caps).squeeze()
        class_prediction = class_prediction.squeeze()
        if self.analysis == False:
           return class_prediction, entropy
        return class_prediction, entropies