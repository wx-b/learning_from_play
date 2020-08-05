# Ok, lets collect data from preprogrammed pick

import gym
import pandaRL
import numpy as np
import os
import shutil
from tqdm import tqdm

env = gym.make('pandaPlay-v0')
#env.render(mode='human')
env.reset()

open_gripper = np.array([0.04])
closed_gripper = np.array([0.01])
p = env.panda.bullet_client
which_object = 0

#--These two skills are used both in picking and pushing, use the offset to push by going next to
def go_above(env, obj_number, offset = np.zeros(3)):
    desired_position = env.panda.calc_environment_state()[obj_number]['pos'] + np.array([0, 0.05, 0]) + offset
    current_position = env.panda.calc_actor_state()['pos']
    current_orn = env.panda.calc_actor_state()['orn']
    action = np.concatenate([desired_position - current_position, np.array(env.panda.default_arm_orn)-np.array(current_orn), open_gripper])
    return action

def descend_push(env, obj_number, offset = np.zeros(3)):
    desired_position = env.panda.calc_environment_state()[obj_number]['pos'] + np.array([0, -0.05, 0]) + offset
    current_position = env.panda.calc_actor_state()['pos']
    current_orn = env.panda.calc_actor_state()['orn']
    action = np.concatenate([desired_position - current_position, np.array(env.panda.default_arm_orn)-np.array(current_orn), closed_gripper])
    return action



# Skills only used for picking
def descend(env, obj_number, offset = np.zeros(3)):
    desired_position = env.panda.calc_environment_state()[obj_number]['pos'] + np.array([0, -0.015, 0]) + offset
    current_position = env.panda.calc_actor_state()['pos']
    current_orn = env.panda.calc_actor_state()['orn']
    action = np.concatenate([desired_position - current_position, np.array(env.panda.default_arm_orn)-np.array(current_orn), open_gripper])
    return action

def close(env, obj_number, offset = np.zeros(3)):
    desired_position = env.panda.calc_environment_state()[obj_number]['pos']
    current_position = env.panda.calc_actor_state()['pos']
    current_orn = env.panda.calc_actor_state()['orn']
    action = np.concatenate([desired_position - current_position, np.array(env.panda.default_arm_orn)-np.array(current_orn), closed_gripper])
    return action

def lift(env, obj_number, offset = np.zeros(3)):
    desired_position = env.panda.calc_environment_state()[obj_number]['pos']
    desired_position[1] +=  0.02
    current_position = env.panda.calc_actor_state()['pos']
    current_orn = env.panda.calc_actor_state()['orn']
    action = np.concatenate([desired_position - current_position, np.array(env.panda.default_arm_orn)-np.array(current_orn), closed_gripper])
    return action

def take_to(env, position, offset = np.zeros(3)):
    desired_position = position
    current_position = env.panda.calc_actor_state()['pos']
    current_orn = env.panda.calc_actor_state()['orn']
    action = np.concatenate([desired_position - current_position, np.array(env.panda.default_arm_orn)-np.array(current_orn), closed_gripper])*0.5
    return action



def pick_to(env, t, o, counter, acts,obs,goals,ags,cagb,fpsb):
    global which_object
    times = np.array([0.33, 0.66, 1.0, 1.3, 2.0, 2.2]) + t
    states = [go_above, descend, close, lift, take_to, go_above]


    take_to_pos = np.random.uniform(env.goal_lower_bound, env.goal_upper_bound)
    goal = env.panda.goal
    goal[which_object*3:(which_object+1)*3] = take_to_pos
    env.panda.reset_goal_pos(goal)
    data = peform_action(env, t, o, counter, acts,obs,goals,ags,cagb,fpsb, times, states, goal=take_to_pos, obj_number=which_object)
    which_object = not which_object # flip which object we are playing with
    return data


def peform_action(env, t, o, counter, acts,obs,goals,ags,cagb,fpsb, times, states, goal=None, offset=np.zeros(3), obj_number=0):
    state_pointer = 0
    while (t < times[state_pointer]):
        if state_pointer == 4:
            action = states[state_pointer](env, goal, offset = np.zeros(3))
        else:
            action = states[state_pointer](env, obj_number=obj_number, offset=offset)
        if not debugging:
            p.saveBullet(example_path + '/env_states/' + str(counter) + ".bullet")
        counter += 1  # little counter for saving the bullet states
        o2, r, d, _ = env.step(action)
        if d:
            print('Env limits exceeded')
            return {'success':0, 't':t}
        acts.append(action), obs.append(o['observation']), goals.append(o['desired_goal']), ags.append(
            o2['achieved_goal']), \
        cagb.append(o2['controllable_achieved_goal']), fpsb.append(o2['full_positional_state'])
        o = o2

        t += dt
        if t >= times[state_pointer]:
            state_pointer += 1
            if state_pointer > len(times)-1:
                break

    return {'last_obs': o, 'success': 1, 't':t, 'counter':counter}




debugging = False

dt = 0.04

action_buff = []
observation_buff = []
desired_goals_buff = []
achieved_goals_buff = []
controllable_achieved_goal_buff = []
full_positional_state_buff = []


base_path = 'collected_data/play_demos/'
try:
    os.makedirs(base_path)
except:
    print('Folder already exists')

demo_count = len(list(os.listdir(base_path)))

activities = [pick_to]#, push_directionally]
#activities = [push_directionally]

play_len = 120

for i in tqdm(range(0, 40)):
    o = env.reset()
    t = 0

    acts, obs, goals, ags, cagb, fpsb = [], [], [], [], [], []
    example_path = base_path + str(demo_count)
    if not debugging:
        os.makedirs(example_path)
        os.makedirs(example_path + '/env_states')
    counter = 0

    #pbar = tqdm(total=play_len)
    while(t < play_len):
        activity_choice = np.random.choice(len(activities))
        result = activities[activity_choice](env, t, o, counter, acts,obs,goals,ags,cagb,fpsb)
        if not result['success']:
            break
        #pbar.update(result['t'] - t)
        t = result['t']
        counter = result['counter']
        o = result['last_obs']


    if t>(play_len/2): #reasonable length with some play interaction
        if not debugging:

            action_buff.append(acts), observation_buff.append(obs), desired_goals_buff.append(
                goals), achieved_goals_buff.append(ags), \
            controllable_achieved_goal_buff.append(cagb), full_positional_state_buff.append(fpsb)

            np.savez(base_path + str(demo_count) + '/data', acts=acts, obs=obs,
                     desired_goals=goals,
                     achieved_goals=ags,
                     controllable_achieved_goals=cagb,
                     full_positional_states=fpsb)
            demo_count += 1
    else:
        print('Demo failed')
        # delete the folder with all the saved states within it
        if not debugging:
            shutil.rmtree(base_path + str(demo_count))










#
# def push_directionally(env, t, o, counter, acts,obs,goals,ags,cagb,fpsb):
#     times = np.array([0.5, 1.0, 1.4]) + t
#     states = [go_above, descend_push, go_above]
#     # choose a random point in a circle around the block
#     alpha = np.random.random(1)*2*np.pi
#     r = 0.03
#     x,z = r * np.cos(alpha), r * np.sin(alpha)
#     offset = np.array([x,0,z])
#
#
#     return peform_action(env, t, o, counter, acts, obs, goals, ags, cagb, fpsb, times, states, offset=offset)