#!/usr/bin/env python3
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import copy
import math
import os
os.environ['MUJOCO_GL'] = 'egl'
import sys
import time
import pickle as pkl

from video import VideoRecorder
from logger import Logger
from replay_buffer import ReplayBuffer
import utils

import dmc2gym
import hydra


class Workspace(object):
    def __init__(self, cfg):
        self.work_dir = os.getcwd()
        print(f'workspace: {self.work_dir}')

        self.cfg = cfg

        self.logger = Logger(self.work_dir,
                             save_tb=cfg.log_save_tb,
                             log_frequency=cfg.log_frequency,
                             agent=cfg.agent.name)

        utils.set_seed_everywhere(cfg.seed)
        self.device = torch.device(cfg.device)
        self.env = utils.make_env(cfg)

        cfg.agent.params.obs_dim = self.env.observation_space.shape[0]
        cfg.agent.params.action_dim = self.env.action_space.shape[0]
        cfg.agent.params.action_range = [
            float(self.env.action_space.low.min()),
            float(self.env.action_space.high.max())
        ]
        self.agent = hydra.utils.instantiate(cfg.agent)

        self.replay_buffer = ReplayBuffer(self.env.observation_space.shape,
                                          self.env.action_space.shape,
                                          int(cfg.replay_buffer_capacity),
                                          self.device)

        self.temp_buffer = ReplayBuffer(self.env.observation_space.shape,
                                          self.env.action_space.shape,
                                          int(cfg.temp_buffer_capacity),
                                          self.device)


        self.video_recorder = VideoRecorder(
            self.work_dir if cfg.save_video else None)
        self.step = 0

    def evaluate(self):
        average_episode_reward = 0
        for episode in range(self.cfg.num_eval_episodes):
            obs = self.env.reset()
            self.agent.reset()
            self.video_recorder.init(enabled=(episode == 0))
            done = False
            episode_reward = 0
            while not done:
                with utils.eval_mode(self.agent):
                    action = self.agent.act(obs, sample=False)
                obs, reward, done, _ = self.env.step(action)
                self.video_recorder.record(self.env)
                episode_reward += reward

            average_episode_reward += episode_reward
            self.video_recorder.save(f'{self.step}.mp4')
        average_episode_reward /= self.cfg.num_eval_episodes
        self.logger.log('eval/episode_reward', average_episode_reward,
                        self.step)
        self.logger.dump(self.step)

    def run(self):

        print('\033[1;33m-----------------------')
        print(f"\033[1;33m task_name: {self.cfg.env}")
        print(f"\033[1;33m env_steps: {self.cfg.num_train_steps}")
        print(f"\033[1;33m seeds: {self.cfg.seed}")
        print(f"\033[1;33m replay ratio: {self.cfg.replay_ratio}")
        print(f"\033[1;33m temp_buffer size: {self.cfg.temp_buffer_capacity}")
        print('\033[1;33m-----------------------')

        episode, episode_reward, done = 0, 0, True
        start_time = time.time()
        while self.step < self.cfg.num_train_steps:
            if done:
                if self.step > 0:
                    self.logger.log('train/duration',
                                    time.time() - start_time, self.step)
                    start_time = time.time()
                    self.logger.dump(
                        self.step, save=(self.step > self.cfg.num_seed_steps))

                # evaluate agent periodically
                if self.step > 0 and self.step % self.cfg.eval_frequency == 0:
                    self.logger.log('eval/episode', episode, self.step)
                    self.evaluate()
                    # self.agent.reset_network()
                    # print('成功重置网络!')
                    # self.evaluate()

                self.logger.log('train/episode_reward', episode_reward,
                                self.step)

                obs = self.env.reset()
                self.agent.reset()
                done = False
                episode_reward = 0
                episode_step = 0
                episode += 1
                self.logger.log('train/episode', episode, self.step)

            # 网络初始化
            if self.step > 0 and ((self.step * self.cfg.replay_ratio) % self.cfg.reset_frequency) == 0:
                self.agent.reset_network()
                print('网络重新初始化!')
                # 重置网络后预训练
                for nums in range(1000):
                    self.agent.updateAll(self.replay_buffer, self.temp_buffer, self.logger, self.step)
                print('预训练完成!')

            # if (self.step == self.cfg.num_seed_steps):
            #     print('update with init experience')
            #     for num in range(100):
            #         self.agent.updateInit(self.replay_buffer, self.logger, self.step)

            # sample action for data collection
            if self.step < self.cfg.num_seed_steps:
                action = self.env.action_space.sample()
            else:
                with utils.eval_mode(self.agent):
                    action = self.agent.act(obs, sample=True)

            # run training update
            # temp_buffer_capacity = 1000

            if (self.step >= self.cfg.num_seed_steps):
                # print('replay buffer size: ',self.replay_buffer.idx)
                # print('steps: ', self.step)
                # print('update with on-policy and off-policy data')
                # for update_times in range(20):
                # self.agent.update(self.replay_buffer, self.temp_buffer, self.logger, self.step)
                for replay in range(self.cfg.replay_ratio):
                    self.agent.updateAll(self.replay_buffer, self.temp_buffer, self.logger, self.step)
            # if (self.step > self.cfg.num_seed_steps) and (self.step % 500 == 0):
            #     self.agent.updateAll(self.replay_buffer, self.temp_buffer, self.logger, self.step)

            next_obs, reward, done, _ = self.env.step(action)

            # allow infinite bootstrap
            done = float(done)

            done_no_max = 0 if episode_step + 1 == self.env._max_episode_steps else done
            episode_reward += reward

            # 填满初始5000steps后，将temp_buffer 输入 replay_buffer
            # if (self.step >= self.cfg.num_seed_steps) and (self.step % self.cfg.temp_buffer_capacity == 0):
            #     # print('before: ',self.replay_buffer.idx)
            #     for idx in range(self.cfg.temp_buffer_capacity):
            #         self.replay_buffer.add(self.temp_buffer.obses[idx],
            #                                self.temp_buffer.actions[idx],
            #                                self.temp_buffer.rewards[idx],
            #                                self.temp_buffer.next_obses[idx],
            #                                self.temp_buffer.not_dones[idx],
            #                                self.temp_buffer.not_dones_no_max[idx])
                # print('after: ', self.replay_buffer.idx)
            # if self.step < self.cfg.num_seed_steps:
            self.replay_buffer.add(obs, action, reward, next_obs, done, done_no_max)

            self.temp_buffer.add(obs, action, reward, next_obs, done, done_no_max)
            obs = next_obs
            episode_step += 1
            self.step += 1

        self.logger.log('eval/episode', episode, self.step)
        self.evaluate()
        self.logger.log('train/episode_reward', episode_reward, self.step)


@hydra.main(config_path='config/train.yaml')
def main(cfg):
    workspace = Workspace(cfg)
    workspace.run()


if __name__ == '__main__':
    main()