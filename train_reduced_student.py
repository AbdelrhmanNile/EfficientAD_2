import torch
import torch.nn.functional as F
import random
from torch import optim
from torch.utils.data import Dataset, DataLoader
import numpy as np
from torchvision import transforms
from models import Teacher,Student,AutoEncoder
from torch.optim.lr_scheduler import StepLR
from data_loader import TrainImageOnlyDataset,ImageNetDataset,MVTecDataset,load_infinite
import tqdm
import os.path as osp
import shutil
import cv2
import pdb
import os
from sklearn.metrics import roc_auc_score,average_precision_score

from itertools import cycle
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = False
torch.backends.cudnn.enabled = True
from PIL import Image

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv2d') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)

class Reduced_Student_Teacher(object):
    def __init__(self,label,mvtech_dir,imagenet_dir,ckpt_path,model_size='S',batch_size=1,channel_size=384,score_in_mid_size=224,resize=256,fmap_size=256,print_freq=100) -> None:
        self.label = label
        self.mvtech_dir = osp.join(mvtech_dir,label)
        self.imagenet_dir = imagenet_dir
        self.ckpt_path = ckpt_path
        self.student = Student(model_size)
        self.student = self.student.cuda()
        # self.student.apply(weights_init)
        self.teacher = Teacher(model_size)
        self.load_pretrain_teacher()
        self.ae = AutoEncoder()
        self.ae = self.ae.cuda()
        # self.ae.apply(weights_init)
        self.score_in_mid_size=score_in_mid_size
        self.resize = resize
        self.channel_size = channel_size
        self.fmap_size = (fmap_size,fmap_size)
        self.channel_mean,self.channel_std = None,None
        self.batch_size = batch_size
        self.print_freq = print_freq
        self.data_transforms = transforms.Compose([
                        transforms.Resize((resize, resize)),
                        transforms.ToTensor(),
                        ])
        self.gt_transforms = transforms.Compose([
                        transforms.Resize((resize, resize)),
                        transforms.ToTensor()])
        self.data_transforms_imagenet = transforms.Compose([ #We obtain an image P ∈ R 3×256×256 from ImageNet by choosing a random image,
                        transforms.Resize((512, 512)), #resizing it to 512 × 512,
                        transforms.RandomGrayscale(p=0.3), #converting it to gray scale with a probability of 0.3
                        transforms.CenterCrop((256,256)), # and cropping the center 256 × 256 pixels
                        transforms.ToTensor(),
                        ])

    def load_pretrain_teacher(self):
        self.teacher.load_state_dict(torch.load(self.ckpt_path+'/best_teacher.pth'))
        self.teacher = self.teacher.cuda()
        self.teacher.eval()
        for parameters in self.teacher.parameters():
            parameters.requires_grad = False
        print('load teacher model from {}'.format(self.ckpt_path+'/best_teacher.pth'))

    def global_channel_normalize(self,dataloader):
        num = 0
        x = torch.empty(0)
        for item in tqdm.tqdm(dataloader):
            if num>500:
                break
            num +=1
            ldist = item['image'].cuda()
            # ldist = iteration['image'].cuda()
            y = self.teacher(ldist).detach().cpu()
            # ymean = y.mean(dim=[2,3],keepdim=True)
            # ystd = y.std(dim=[2,3],keepdim=True)
            x = torch.cat((x,y),0)
            # x_std = torch.cat((x_std,y),0)
        self.channel_mean = x.mean(dim=[0,2,3],keepdim=True).cuda()
        self.channel_std = x.std(dim=[0,2,3],keepdim=True).cuda()
        return self.channel_mean,self.channel_std

    def choose_random_aug_image(self,image):
        aug_index = random.choice([1,2,3])
        # Sample an augmentation coefficient λ from the uniform distribution U(0.8, 1.2)
        coefficient = random.uniform(0.8,1.2)
        if aug_index == 1:
            img_aug = transforms.functional.adjust_brightness(image,coefficient)
        elif aug_index == 2:
            img_aug = transforms.functional.adjust_contrast(image,coefficient)
        elif aug_index == 3:
            img_aug = transforms.functional.adjust_saturation(image,coefficient)
        return img_aug

    # def squard_diffence(self,Y0,Y1):

    def loss_st(self,image,imagenet_iterator,teacher:Teacher,student:Student):
        with torch.no_grad():
            t_pdn_out = teacher(image)
            b,c,h,w = t_pdn_out.shape
            normal_t_out = (t_pdn_out-self.channel_mean)/self.channel_std
        s_pdn_out = student(image)
        s_pdn_out = s_pdn_out[:, :384, :, :]
        distance_s_t = torch.pow(normal_t_out-s_pdn_out,2)
        dhard = torch.quantile(distance_s_t[:8,:,:,:],0.999)
        hard_data = distance_s_t[distance_s_t>=dhard]
        Lhard = torch.mean(hard_data)
        image_p = next(imagenet_iterator)
        s_imagenet_out = student(image_p[0].cuda())
        N = torch.mean(torch.pow(s_imagenet_out[:, :384, :, :],2))
        loss_st = Lhard + N
        return loss_st
    
    def loss_ae(self,image,teacher:Teacher,student:Student,autoencoder:AutoEncoder):
        aug_img = self.choose_random_aug_image(image=image)
        aug_img = aug_img.cuda()
        with torch.no_grad():
            t_out = teacher(aug_img)
            normal_t_out = (t_out-self.channel_mean)/self.channel_std
        ae_out = autoencoder(aug_img)
        s_pdn_out = student(aug_img)
        s_pdn_out = s_pdn_out[:, 384:, :, :]
        distance_ae = torch.pow(normal_t_out-ae_out,2)
        distance_stae = torch.pow(ae_out-s_pdn_out,2)
        LAE = torch.mean(distance_ae)
        LSTAE = torch.mean(distance_stae)
        return LAE,LSTAE
    
    def caculate_channel_std(self,dataloader):
        channel_std_ckpt = "{}/{}_good_dataset_channel_std.pth".format(self.ckpt_path,self.label)
        if osp.isfile(channel_std_ckpt):
            channel_std = torch.load(channel_std_ckpt)
            self.channel_mean = channel_std['mean'].cuda()
            self.channel_std = channel_std['std'].cuda()
        else:
            self.channel_mean,self.channel_std = self.global_channel_normalize(dataloader)
            print('channel mean:{}'.format(self.channel_mean.shape),'channel std:{}'.format(self.channel_std.shape))
            channel_std = {
                'mean':self.channel_mean,
                'std':self.channel_std
            }
            torch.save(channel_std,channel_std_ckpt)

    def train(self,iterations=70000):
        # Initialize Adam [29] with a learning rate of 10−4 and a weight decay of 10−5 for the parameters of S and A

        dataset = MVTecDataset(
                        root=self.mvtech_dir,
                        transform=self.data_transforms,
                        gt_transform=self.gt_transforms,
                        phase='train'
                        )
        dataloader = DataLoader(dataset,batch_size=self.batch_size,shuffle=True,num_workers=4, pin_memory=True)
        print('load train dataset:length:{}'.format(len(dataset)))
        imagenet = ImageNetDataset(imagenet_dir=self.imagenet_dir,transform=self.data_transforms_imagenet)
        imagenet_loader = DataLoader(imagenet,batch_size=1,shuffle=True,num_workers=4, pin_memory=True)
        # len_traindata = len(dataset)
        imagenet_iterator = cycle(iter(imagenet_loader))
        self.caculate_channel_std(dataloader)
        optimizer = optim.Adam(list(self.student.parameters())+list(self.ae.parameters()),lr=0.0001,weight_decay=0.00001)
        scheduler = torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=int(0.95 * iterations), gamma=0.1)
        best_auroc = 0
        dataloader = load_infinite(dataloader)
        eval_dataset = MVTecDataset(
                        root=self.mvtech_dir,
                        transform=self.data_transforms,
                        gt_transform=self.gt_transforms,
                        phase='test'
                        )
        eval_dataloader = DataLoader(eval_dataset,batch_size=1,shuffle=True)
        print('start train iter:',iterations)
        for i_batch in range(iterations):
            sample_batched = next(dataloader)
            image = sample_batched['image'].cuda()
            self.student.train()
            self.ae.train()
            loss_st = self.loss_st(image,imagenet_iterator,self.teacher,self.student)
            LAE,LSTAE = self.loss_ae(image,self.teacher,self.student,self.ae)
            loss_total = loss_st + LAE + LSTAE

            optimizer.zero_grad()
            loss_total.backward()
            optimizer.step()
            scheduler.step()
            if i_batch % self.print_freq == 0:
                print("label:{},batch:{}/{},loss_total:{:.4f},loss_st:{:.4f},loss_ae:{:.4f},loss_stae:{:.4f}".format(
                    self.label,i_batch,iterations,loss_total.item(),loss_st.item(),LAE.item(),LSTAE.item()))

                self.qa_st,self.qb_st,self.qa_ae,self.qb_ae = self.map_norm_quantiles()
                auroc = self.eval(eval_dataloader)
                if auroc > best_auroc:
                    best_auroc = auroc
                    print('saving model in {} at auroc:{:.4f}'.format(self.ckpt_path,auroc))
                    torch.save(self.student.state_dict(),'{}/{}_student.pth'.format(self.ckpt_path,self.label))
                    torch.save(self.ae.state_dict(),'{}/{}_autoencoder.pth'.format(self.ckpt_path,self.label))
                    quantiles = {
                        'qa_st':self.qa_st,
                        'qb_st':self.qb_st,
                        'qa_ae':self.qa_ae,
                        'qb_ae':self.qb_ae,
                        'std':self.channel_std.cpu().numpy(),
                        'mean':self.channel_mean.cpu().numpy()
                    }
                    np.save('{}/{}_quantiles.npy'.format(self.ckpt_path,self.label),quantiles)

    def eval(self,eval_dataloader):
        scores = []
        gts = []
        for sample_batched in tqdm.tqdm(eval_dataloader):
            gts.append(sample_batched['label'].item())
            combined_map,image_score = self.infer_single(sample_batched)
            scores.append(image_score.item())
        gtnp = np.array(gts)
        scorenp = np.array(scores)
        auroc = roc_auc_score(gtnp,scorenp)
        return auroc

    def infer_single(self,sample_batched):
        img = sample_batched['image']
        img = img.cuda()
        with torch.no_grad():
            teacher_output = self.teacher(img)
            student_output = self.student(img)
            ae_output = self.ae(img)
        #3: Split the student output into Y ST ∈ R 384×64×64 and Y STAE ∈ R 384×64×64 as above
        y_st = student_output[:, :384, :, :]
        y_stae = student_output[:, -384:, :, :]

        normal_teacher_output = (teacher_output-self.channel_mean)/self.channel_std

        distance_st = torch.pow(normal_teacher_output-y_st,2)
        distance_stae = torch.pow(ae_output-y_stae,2)

        fmap_st = torch.mean(distance_st,dim=1,keepdim=True)
        fmap_stae = torch.mean(distance_stae,dim=1,keepdim=True)
        fmap_st = F.interpolate(fmap_st,size=(256,256),mode='bilinear')
        fmap_stae = F.interpolate(fmap_stae,size=(256,256),mode='bilinear')
        normalized_mst = (0.1*(fmap_st-self.qa_st))/(self.qb_st-self.qa_st)
        normalized_mae = (0.1*(fmap_stae-self.qa_ae))/(self.qb_ae-self.qa_ae)
        combined_map = 0.5*normalized_mst+0.5*normalized_mae
        score_start = (self.resize-self.score_in_mid_size)//2
        image_score = torch.max(combined_map[:,:,
            score_start:score_start+self.score_in_mid_size,
            score_start:score_start+self.score_in_mid_size
        ])
        return combined_map,image_score

    def map_norm_quantiles(self):
        xst,xae = [],[]
        dataset = MVTecDataset(
                        root=self.mvtech_dir,
                        transform=self.data_transforms,
                        gt_transform=self.gt_transforms,
                        phase='train'
                        )
        dataloader = DataLoader(dataset,batch_size=1,shuffle=True)
        self.student.eval()
        self.ae.eval()
        self.teacher.eval()
        for i_batch, sample_batched in enumerate(dataloader):
            sample_batched = sample_batched['image'].cuda()
            with torch.no_grad():
                t_out = self.teacher(sample_batched)
                s_out = self.student(sample_batched)
                ae_out = self.ae(sample_batched)
            #48: Split the student output into Y ST ∈ R 384×64×64 and Y STAE ∈ R 384×64×64 as above
            y_st = s_out[:, :384, :, :]
            y_stae = s_out[:, 384:, :, :]
            # normal_t_out = self.compute_normalize_teacher_out(t_out)
            normal_t_out = (t_out-self.channel_mean)/self.channel_std
            distance_s_t = torch.pow(normal_t_out-y_st,2)
            distance_stae = torch.pow(ae_out-y_stae,2)
            anomaly_map_st_by_c = torch.mean(distance_s_t,dim=1)
            anomaly_map_stae_by_c = torch.mean(distance_stae,dim=1)
            anomaly_map_st = F.interpolate(anomaly_map_st_by_c.unsqueeze(0),
                                            size=self.fmap_size,mode='bilinear')
            anomaly_map_ae = F.interpolate(anomaly_map_stae_by_c.unsqueeze(0),
                                            size=self.fmap_size,mode='bilinear')
            xst.append(anomaly_map_st.detach().cpu().numpy())
            xae.append(anomaly_map_ae.detach().cpu().numpy())
        qa_st = np.percentile(np.concatenate(xst),90)
        qb_st = np.percentile(np.concatenate(xst),99.5)
        qa_ae = np.percentile(np.concatenate(xae),90)
        qb_ae = np.percentile(np.concatenate(xae),99.5)
        return qa_st,qb_st,qa_ae,qb_ae

if __name__ == '__main__':
    ckpt = 'ckptSmall'
    if not os.path.exists(ckpt):
        os.makedirs(ckpt)
    rst = Reduced_Student_Teacher(
        label='zipper',
        mvtech_dir="data/MVTec_AD/",
        imagenet_dir="data/ImageNet/",
        ckpt_path=ckpt,
        model_size='S',
        batch_size=1,
    )
    rst.train(iterations=50000)





        
