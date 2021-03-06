import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf

from tensorflow.keras.losses import MeanSquaredError
from tensorflow.keras.metrics import Accuracy
from tensorflow.keras.optimizers import SGD, Adam
from tensorflow.keras.utils import Progbar
from tensorflow.distribute import ReduceOp
import tensorflow_probability as tfp
tfd = tfp.distributions
import tensorflow_addons as tfa
from tensorflow_addons.optimizers import AdamW
import lfp
import os
import wandb
import json

class BetaScheduler():
    def __init__(self, schedule='constant', beta=0.0, beta_max=1.0, max_steps=1e4,
                    cycles=10, duty_cycle=0.5, plot=True):
        self.schedule = schedule
        self.beta_min = beta
        self.beta_max = beta_max
        self.max_steps = max_steps
        self.cycles = cycles
        self.duty_cycle = duty_cycle

        if schedule=='constant':
            self.scheduler = lambda s: tf.ones_like(s, dtype=tf.float32)*beta
        elif schedule=='linear':
            self.scheduler = self.linear_schedule
        elif schedule=='quadratic':
            self.scheduler = self.quadratic_schedule
        elif schedule=='cyclic':
            self.scheduler = self.cyclic_schedule
        else:
            raise NotImplementedError()
        if plot: self._plot_schedule()
    
    def linear_schedule(self, step):
        beta = self.beta_min + (step) * (self.beta_max-self.beta_min)/self.max_steps
        return tf.clip_by_value(float(beta), self.beta_min, self.beta_max, name='beta_linear')

    def quadratic_schedule(self, step):
        ''' y = (b1-b0)/n^2 * x^2 + b0 '''
        beta = self.beta_min + (step)**2 * (self.beta_max-self.beta_min)/self.max_steps**2
        return tf.clip_by_value(float(beta), self.beta_min, self.beta_max, name='beta_quadratic')

    def cyclic_schedule(self, step):
        period = self.max_steps // self.cycles
        step = step % period # map step to cycle
        if step < period * self.duty_cycle:
            # linear regime
            beta = self.beta_min + (step) * (self.beta_max-self.beta_min)/(period*self.duty_cycle)
        else:
            # constant regime
            beta = self.beta_max
        return tf.clip_by_value(float(beta), self.beta_min, self.beta_max, name='beta_cyclic')

    def _plot_schedule(self):
        ts = np.arange(self.max_steps, step=100)
        plt.plot(ts, [self.scheduler(t) for t in ts])
        plt.xlabel('Steps')
        plt.ylabel('Beta')


class LFPTrainer():



    def __init__(self, dataloader, actor, probabilistic, encoder=None, planner=None,
                 distribute_strategy=None, learning_rate='3e-4', plan_lr_multiplier=1, clipnorm=5.0, gcbc=False):
        self.actor = actor
        self.encoder = encoder
        self.planner = planner
        self.distribute_strategy = distribute_strategy
        self.probabilistic = probabilistic
        self.gcbc = gcbc
        self.window_size = dataloader.window_size
        self.quaternion_act = dataloader.quaternion_act
        self.batch_size = dataloader.batch_size




        with self.distribute_strategy.scope():
            # self.actor_optimizer = Adam(learning_rate=learning_rate, global_clipnorm=clipnorm)
            # self.encoder_optimizer = Adam(learning_rate=learning_rate, global_clipnorm=clipnorm)
            # self.planner_optimizer = Adam(learning_rate=learning_rate*plan_lr_multiplier, global_clipnorm=clipnorm)
            self.global_optimizer = Adam(learning_rate=learning_rate)

            self.actor_grad_len  = len(self.actor.trainable_variables)
            if not self.gcbc:
                self.encoder_grad_len = len(self.encoder.trainable_variables)
                self.planner_grad_len = len(self.planner.trainable_variables)

            # Metrics
            self.metrics = {}
            self.metrics['train_loss'] = tf.keras.metrics.Mean(name='train_loss')
            self.metrics['actor_grad_norm'] = tf.keras.metrics.Mean(name='actor_grad_norm')
            self.metrics['actor_grad_norm_clipped'] = tf.keras.metrics.Mean(name='actor_grad_clipped')
            self.metrics['valid_loss'] = tf.keras.metrics.Mean(name='valid_loss')
            self.metrics['valid_position_loss'] = tf.keras.metrics.Mean(name='valid_position_loss')
            self.metrics['valid_max_position_loss'] = lfp.metric.MaxMetric(name='valid_max_position_loss')
            self.metrics['valid_rotation_loss'] = tf.keras.metrics.Mean(name='valid_rotation_loss')
            self.metrics['valid_max_rotation_loss'] = lfp.metric.MaxMetric(name='valid_max_rotation_loss')
            self.metrics['valid_gripper_loss'] = tf.keras.metrics.Mean(name='valid_gripper_loss')
            self.metrics['global_grad_norm'] = tf.keras.metrics.Mean(name='global_grad_norm')

            def compute_loss(labels, predictions, mask, seq_lens):
                if self.probabilistic:
                    per_example_loss = self.nll_action_loss(labels, predictions) * mask
                else:
                    per_example_loss = self.mae_action_loss(labels, predictions) * mask

                per_example_loss = tf.reduce_sum(per_example_loss, axis=1) / seq_lens  # take mean along the timestep
                return tf.nn.compute_average_loss(per_example_loss, global_batch_size=self.batch_size)


            def compute_MAE(labels, predictions, mask, seq_lens, weightings=None):
                per_example_loss = self.mae_action_loss(labels, predictions) * mask
                per_example_loss = tf.reduce_sum(per_example_loss, axis=1) / seq_lens  # take mean along the timestep
                return tf.nn.compute_average_loss(per_example_loss, global_batch_size=self.batch_size)


            def compute_regularisation_loss(plan, encoding):
                # Reverse KL(enc|plan): we want planner to map to encoder (weighted by encoder)
                reg_loss = tfp.distributions.kl_divergence(encoding, plan)
                return tf.nn.compute_average_loss(reg_loss, global_batch_size=self.batch_size)

            # Losses # done this way so that they are in stratey scope
            self.nll_action_loss = lambda y, p_y: tf.reduce_sum(-p_y.log_prob(y), axis=2)
            self.mae_action_loss = tf.keras.losses.MeanAbsoluteError(reduction=tf.keras.losses.Reduction.NONE)
            self.mse_action_loss = tf.keras.losses.MeanSquaredError(reduction=tf.keras.losses.Reduction.NONE)
            self.compute_loss = compute_loss
            self.compute_MAE = compute_MAE
            self.compute_regularisation_loss = compute_regularisation_loss



            if not self.gcbc:
                self.metrics['train_reg_loss'] = tf.keras.metrics.Mean(name='train_reg_loss')
                self.metrics['train_act_with_enc_loss'] = tf.keras.metrics.Mean(name='train_act_with_enc_loss')
                self.metrics['train_act_with_plan_loss'] = tf.keras.metrics.Mean(name='train_act_with_plan_loss')
                self.metrics['encoder_grad_norm'] = tf.keras.metrics.Mean(name='encoder_grad_norm')
                self.metrics['planner_grad_norm'] = tf.keras.metrics.Mean(name='planner_grad_norm')
                self.metrics['encoder_grad_norm_clipped'] = tf.keras.metrics.Mean(name='encoder_grad_norm_clipped')
                self.metrics['planner_grad_norm_clipped'] = tf.keras.metrics.Mean(name='planner_grad_norm_clipped')
                self.metrics['valid_reg_loss'] = tf.keras.metrics.Mean(name='valid_reg_loss')
                self.metrics['valid_act_with_enc_loss'] = tf.keras.metrics.Mean(name='valid_act_with_enc_loss')
                self.metrics['valid_act_with_plan_loss'] = tf.keras.metrics.Mean(name='valid_act_with_plan_loss')


    # Now outside strategy .scope
    def train_step(self, inputs, beta, prev_global_grad_norm):
        # Todo: figure out mask and seq_lens for new dataset
        states, actions, goals, seq_lens, mask = inputs['obs'], inputs['acts'], inputs['goals'], inputs['seq_lens'], \
                                                 inputs['masks']
        if self.gcbc:
            with tf.GradientTape() as actor_tape:
                distrib = self.actor([states, goals])
                loss = self.compute_loss(actions, distrib, mask, seq_lens)
                gradients = actor_tape.gradient(loss, self.actor.trainable_variables)
                self.actor_optimizer.apply_gradients(zip(gradients, self.actor.trainable_variables))
        else:
            with tf.GradientTape() as tape:#, tf.GradientTape() as encoder_tape, tf.GradientTape() as planner_tape:
                encoding = self.encoder([states, actions])
                plan = self.planner([states[:, 0, :], goals[:, 0, :]])  # the final goals are tiled out over the entire non masked sequence, so the first timestep is the final goal.
                z_enc = encoding.sample()
                z_plan = plan.sample()
                z_enc_tiled = tf.tile(tf.expand_dims(z_enc, 1), (1, self.window_size, 1))
                z_plan_tiled = tf.tile(tf.expand_dims(z_plan, 1), (1, self.window_size, 1))

                enc_policy = self.actor([states, z_enc_tiled, goals])
                plan_policy = self.actor([states, z_plan_tiled, goals])

                act_enc_loss = self.compute_loss(actions, enc_policy, mask, seq_lens)
                act_plan_loss = self.compute_loss(actions, plan_policy, mask, seq_lens)
                act_loss = act_enc_loss

                reg_loss = self.compute_regularisation_loss(plan, encoding)

                loss = act_loss + reg_loss * beta

                # # Gradients
                # actor_gradients = actor_tape.gradient(loss, self.actor.trainable_variables)
                # encoder_gradients = encoder_tape.gradient(loss, self.encoder.trainable_variables)
                # planner_gradients = planner_tape.gradient(loss, self.planner.trainable_variables)
                # all_gradients = actor_gradients + encoder_gradients + planner_gradients # concat lists

                gradients = tape.gradient(loss, self.actor.trainable_variables+self.encoder.trainable_variables+self.planner.trainable_variables)

                actor_gradients = gradients[:self.actor_grad_len]
                encoder_gradients =  gradients[self.actor_grad_len:self.actor_grad_len+self.encoder_grad_len]
                planner_gradients = gradients[self.actor_grad_len+self.encoder_grad_len:self.actor_grad_len+self.encoder_grad_len+self.planner_grad_len]

                self.metrics['actor_grad_norm'].update_state(tf.linalg.global_norm(actor_gradients))
                self.metrics['encoder_grad_norm'].update_state(tf.linalg.global_norm(encoder_gradients))
                self.metrics['planner_grad_norm'].update_state(tf.linalg.global_norm(planner_gradients))

                # if the gradient norm is more than 3x the previous one, clip it to the previous norm for stability
                gradients = tf.cond(tf.linalg.global_norm(gradients) > 3 * prev_global_grad_norm,
                                lambda: tf.clip_by_global_norm(gradients, prev_global_grad_norm)[0],
                                lambda: gradients)  # must get[0] as it returns new norm as [1]

                planner_gradients = [g * 10 for g in planner_gradients]

                self.global_optimizer.apply_gradients(zip(gradients, self.actor.trainable_variables+self.encoder.trainable_variables+self.planner.trainable_variables))

                # # Optimizer step
                # self.actor_optimizer.apply_gradients(zip(actor_gradients, self.actor.trainable_variables))
                # self.encoder_optimizer.apply_gradients(zip(encoder_gradients, self.encoder.trainable_variables))
                # self.planner_optimizer.apply_gradients(zip(planner_gradients, self.planner.trainable_variables))

                # Train Metrics
                self.metrics['global_grad_norm'].update_state(tf.linalg.global_norm(gradients))
                self.metrics['train_reg_loss'].update_state(reg_loss)
                self.metrics['train_act_with_enc_loss'].update_state(act_enc_loss)
                self.metrics['train_act_with_plan_loss'].update_state(act_plan_loss)

                self.metrics['actor_grad_norm_clipped'].update_state(tf.linalg.global_norm(actor_gradients))
                self.metrics['encoder_grad_norm_clipped'].update_state(tf.linalg.global_norm(encoder_gradients))
                self.metrics['planner_grad_norm_clipped'].update_state(tf.linalg.global_norm(planner_gradients))

        self.metrics['train_loss'].update_state(loss)

        return loss

    def test_step(self, inputs, beta):
        states, actions, goals, seq_lens, mask = inputs['obs'], inputs['acts'], inputs['goals'], inputs['seq_lens'], \
                                                 inputs['masks']
        if self.quaternion_act:
            # xyz, q1-4, grip
            action_breakdown = [3, 4, 1]
        else:
            action_breakdown = [3, 3, 1]

        if self.gcbc:
            policy = self.actor([states, goals], training=False)
            loss = self.compute_loss(actions, policy, mask, seq_lens)
            if self.probabilistic:
                pos_acts, rot_acts, grip_act = tf.split(policy.sample(), action_breakdown, -1)
            else:
                pos_acts, rot_acts, grip_act = tf.split(policy, action_breakdown, -1)
        else:
            encoding = self.encoder([states, actions])
            plan = self.planner([states[:, 0, :], goals[:, 0, :]])  # the final goals are tiled out over the entire non masked sequence, so the first timestep is the final goal.
            z_enc = encoding.sample()
            z_plan = plan.sample()
            z_enc_tiled = tf.tile(tf.expand_dims(z_enc, 1), (1, self.window_size, 1))
            z_plan_tiled = tf.tile(tf.expand_dims(z_plan, 1), (1, self.window_size, 1))

            enc_policy = self.actor([states, z_enc_tiled, goals])
            plan_policy = self.actor([states, z_plan_tiled, goals])

            act_enc_loss = self.compute_loss(actions, enc_policy, mask, seq_lens)
            act_plan_loss = self.compute_loss(actions, plan_policy, mask, seq_lens)
            act_loss = act_plan_loss

            reg_loss = self.compute_regularisation_loss(plan, encoding)

            # pos, rot, gripper individual losses
            if self.probabilistic:
                pos_acts, rot_acts, grip_act = tf.split(plan_policy.sample(), action_breakdown, -1)
            else:
                pos_acts, rot_acts, grip_act = tf.split(plan_policy, action_breakdown, -1)

            loss = act_loss + reg_loss * beta

        true_pos_acts, true_rot_acts, true_grip_act = tf.split(actions, action_breakdown, -1)

        # Validation Metrics
        self.metrics['valid_reg_loss'].update_state(reg_loss)
        self.metrics['valid_act_with_enc_loss'].update_state(act_enc_loss)
        self.metrics['valid_act_with_plan_loss'].update_state(act_plan_loss)
        self.metrics['valid_position_loss'].update_state(self.compute_MAE(true_pos_acts, pos_acts, mask, seq_lens))
        self.metrics['valid_max_position_loss'](true_pos_acts, pos_acts, mask)
        self.metrics['valid_rotation_loss'].update_state(self.compute_MAE(true_rot_acts, rot_acts, mask, seq_lens))
        self.metrics['valid_max_rotation_loss'](true_rot_acts, rot_acts, mask)
        self.metrics['valid_gripper_loss'].update_state(self.compute_MAE(true_grip_act, grip_act, mask, seq_lens))
        self.metrics['valid_loss'].update_state(loss)

        if self.gcbc:
            return loss
        else:
            return loss, z_enc, z_plan

    @tf.function
    def distributed_train_step(self, dataset_inputs, beta, prev_global_grad_norm):
        per_replica_losses = self.distribute_strategy.run(self.train_step, args=(dataset_inputs, beta, prev_global_grad_norm))
        losses = self.distribute_strategy.reduce(ReduceOp.MEAN, per_replica_losses, axis=None)
        return losses

    @tf.function
    def distributed_test_step(self, dataset_inputs, beta):
        if self.gcbc:
            per_replica_losses = self.distribute_strategy.run(self.test_step, args=(dataset_inputs, beta))
            losses = self.distribute_strategy.reduce(ReduceOp.MEAN, per_replica_losses, axis=None)
            return losses
        else:
            per_replica_losses, ze, zp = self.distribute_strategy.run(self.test_step,
                                                                               args=(dataset_inputs, beta))
            losses = self.distribute_strategy.reduce(ReduceOp.MEAN, per_replica_losses, axis=None)
            return losses, ze.values[0], zp.values[0]

    def save_weights(self, path, config=None, run_id=None, step=""):
        os.makedirs(path, exist_ok=True)

        # Save the config as json
        if config is not None:
            print('Saving training config...')
            with open(f'{path}/config.json', 'w') as f:
                d = vars(config)
                d['run_id'] = run_id
                json.dump(d, f)

        # save timestepped version might be better to save timestepped versions within subfolders?
        # print('Saving model weights...')
        # if step != "":
        #     self.actor.save_weights(f'{path}/actor_{str(step)}.h5')
        #     if not self.gcbc:
        #         self.encoder.save_weights(f'{path}/encoder_{str(step)}.h5')
        #         self.planner.save_weights(f'{path}/planner_{str(step)}.h5')

        # save the latest version
        self.actor.save_weights(f'{path}/actor.h5')
        if not self.gcbc:
            self.encoder.save_weights(f'{path}/encoder.h5')
            self.planner.save_weights(f'{path}/planner.h5')

        os.makedirs(path+'/optimizers', exist_ok=True)
        np.save(f'{path}/optimizers/optimizer.npy', self.global_optimizer.get_weights())
        # save the optimizer state
        # np.save(f'{path}/optimizers/actor_optimizer.npy', self.actor_optimizer.get_weights())
        # if not self.gcbc:
        #     np.save(f'{path}/optimizers/encoder_optimizer.npy', self.encoder_optimizer.get_weights())
        #     np.save(f'{path}/optimizers/planner_optimizer.npy', self.planner_optimizer.get_weights())

    def load_weights(self, path, with_optimizer=False, step=""):
        # IMO better to load timestepped version from subfolders - Todo
        self.actor.load_weights(f'{path}/actor.h5')
        if not self.gcbc:
            self.encoder.load_weights(f'{path}/encoder.h5')
            self.planner.load_weights(f'{path}/planner.h5')
            
        if with_optimizer:
            #self.load_optimizer_state(self.actor_optimizer, f'{path}/optimizers/actor_optimizer.npy', self.actor.trainable_variables)
            self.load_optimizer_state(self.global_optimizer, f'{path}/optimizers/optimizer.npy', self.actor.trainable_variables+self.encoder.trainable_variables+self.planner.trainable_variables)
            # if not self.gcbc:
            #     self.load_optimizer_state(self.encoder_optimizer, f'{path}/optimizers/encoder_optimizer.npy', self.encoder.trainable_variables)
            #     self.load_optimizer_state(self.planner_optimizer, f'{path}/optimizers/planner_optimizer.npy', self.planner.trainable_variables)


    def load_optimizer_state(self, optimizer, load_path, trainable_variables):
        def optimizer_step():
            # need to do this to initialize the optimiser
            # dummy zero gradients
            zero_grads = [tf.zeros_like(w) for w in trainable_variables]
            # save current state of variables
            saved_vars = [tf.identity(w) for w in trainable_variables]

            # Apply gradients which don't do anything
            optimizer.apply_gradients(zip(zero_grads, trainable_variables))

            # Reload variables
            [x.assign(y) for x, y in zip(trainable_variables, saved_vars)]
            return 0.0

        @tf.function
        def distributed_opt_step():
            '''
            Only used for optimizer checkpointing - we need to run a pass to initialise all the optimizer weights. Can't use restore as colab TPUs don't have a local filesystem.
            '''
            per_replica_losses = self.distribute_strategy.run(optimizer_step, args=())
            return self.distribute_strategy.reduce(tf.distribute.ReduceOp.MEAN, per_replica_losses, axis=None)

        # Load optimizer weights
        opt_weights = np.load(load_path, allow_pickle=True)

        # init the optimiser
        distributed_opt_step()
        # Set the weights of the optimizer
        optimizer.set_weights(opt_weights)



# class LFPTrainer_v2():
#     nll_action_loss = lambda y, p_y: tf.reduce_sum(-p_y.log_prob(y), axis=2)
#     mae_action_loss = tf.keras.losses.MeanAbsoluteError(reduction=tf.keras.losses.Reduction.NONE)
#     mse_action_loss = tf.keras.losses.MeanSquaredError(reduction=tf.keras.losses.Reduction.NONE)


#     def __init__(self, optimizer, global_batch_size):
#         self.optimizer = optimizer
#         self.global_batch_size = global_batch_size

#         self.train_loss, self.valid_loss, self.actor_grad_norm, self.encoder_grad_norm, self.planner_grad_norm, \
#         self.actor_grad_norm_clipped, self.encoder_grad_norm_clipped, self.planner_grad_norm_clipped, self.global_grad_norm, \
#         self.test, self.test2,  self.train_act_with_enc_loss, self.train_act_with_plan_loss, self.valid_act_with_enc_loss, self.valid_act_with_plan_loss,\
#         self.train_reg_loss, self.valid_reg_loss, self.valid_position_loss,  self.valid_max_position_loss, self.valid_rotation_loss, self.valid_max_rotation_loss, \
#         self.valid_gripper_loss = lfp.metrics.create_metrics()

#     def compute_loss(self, labels, predictions, mask, seq_lens, weightings=None):
#         if config['num_distribs'] is not None:
#             per_example_loss = self.nll_action_loss(labels, predictions) * mask
#         else:
#             per_example_loss = self.mae_action_loss(labels, predictions) * mask

#         per_example_loss = tf.reduce_sum(per_example_loss, axis=1) / seq_lens  # take mean along the timestep
#         return tf.nn.compute_average_loss(per_example_loss, global_batch_size=self.global_batch_size)


#     def compute_MAE(self, labels, predictions, mask, seq_lens, weightings=None):
#         per_example_loss = self.mae_action_loss(labels, predictions) * mask
#         per_example_loss = tf.reduce_sum(per_example_loss, axis=1) / seq_lens  # take mean along the timestep
#         return tf.nn.compute_average_loss(per_example_loss, global_batch_size=self.global_batch_size)


#     def compute_regularisation_loss(self, plan, encoding):
#         # Reverse KL(enc|plan): we want planner to map to encoder (weighted by encoder)
#         reg_loss = self.tfd.kl_divergence(encoding, plan)  # + KL(plan, encoding)
#         return tf.nn.compute_average_loss(reg_loss, global_batch_size=self.global_batch_size)