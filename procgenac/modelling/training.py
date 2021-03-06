import os
import time
import torch
from procgenac.utils import Storage, save_video, make_env, save_rewards
from procgenac.modelling.utils import save_model, init_model


def training_pipeline(hyperparams, path_to_base, verbose=False):

    # Define device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if verbose:
        before = time.time()
        print(f"Running on {device}")

    # Training env initialization
    env_name = hyperparams.env_name
    env = make_env(
        n_envs=hyperparams.num_envs,
        env_name=env_name,
        start_level=0,
        num_levels=hyperparams.num_levels,
        normalize_reward=False,
    )

    # Define network
    model = init_model(hyperparams, device, env)
    if hyperparams.test_run:
        model.name = "test_" + model.name
    else:
        model.name += f"_id{hyperparams.model_id}"

    # Evaluation env (full distribution with new seed)
    eval_env = make_env(
        n_envs=hyperparams.num_envs,
        env_name=env_name,
        start_level=hyperparams.num_levels,
        num_levels=0,  # full distribution
        normalize_reward=False,
    )

    # Train model
    model, (steps, rewards) = train_model(
        model=model,
        env=env,
        device=device,
        num_epochs=hyperparams.num_epochs,
        batch_size=hyperparams.batch_size,
        adam_lr=hyperparams.adam_lr,
        adam_eps=hyperparams.adam_eps,
        num_steps=hyperparams.num_steps,
        total_steps=hyperparams.total_steps,
        get_test_error=hyperparams.get_test,
        eval_env=eval_env,
        verbose=True,
    )

    # Store training results
    filepath = os.path.join(path_to_base, "results", "rewards", f"{model.name}_{env_name}.csv")
    save_rewards(steps, rewards, filepath=filepath)

    # Save snapshot of current policy
    filepath = os.path.join(path_to_base, "models", f"{model.name}_{env_name}.pt")
    save_model(model, filepath=filepath)

    # Make env for generating a video
    video_env = make_env(
        n_envs=1,
        env_name=env_name,
        start_level=hyperparams.num_levels,
        num_levels=0,
        normalize_reward=False,
    )
    obs = video_env.reset()
    filepath = os.path.join(path_to_base, "results", "videos", f"{model.name}_{env_name}.mp4")
    total_reward, _ = evaluate_model(
        model=model,
        eval_env=video_env,
        obs=obs,
        num_steps=1024,
        video=True,
        video_filepath=filepath,
    )

    if verbose:
        print("Video return:", total_reward.mean(0).item())
        print(f"Time taken: {(time.time() - before)/60:.1f} minutes")


def train_model(
    model,
    env,
    device,
    num_epochs,
    batch_size,
    adam_lr,
    adam_eps,
    num_steps,
    total_steps,
    get_test_error=False,
    eval_env=None,
    verbose=False,
):
    model.to(device=device)

    # Define optimizer
    # these are reasonable values but probably not optimal
    optimizer = torch.optim.Adam(model.parameters(), lr=adam_lr, eps=adam_eps)

    # Define temporary storage
    # we use this to collect transitions during each iteration
    storage = Storage(env.observation_space.shape, num_steps, env.num_envs, device)

    # Run training
    steps = []
    train_rewards = []
    test_rewards = []
    obs = env.reset()
    eval_obs = eval_env.reset()
    step = 0
    n_updates = 0
    update_ite = total_steps // (env.num_envs * num_steps * 100) + 1
    while step < total_steps:

        # Use policy to collect data for num_steps steps
        model.eval()
        for _ in range(num_steps):
            # Use policy
            action, log_prob, value = model.act(obs)

            # Take step in environment
            next_obs, reward, done, info = env.step(action)

            # Store data
            storage.store(obs, action, reward, done, info, log_prob, value)

            # Update current observation
            obs = next_obs

        # Update stats
        if n_updates % update_ite == 0:
            steps.append(step)
            train_rewards.append(storage.get_reward())
            if verbose:
                print(
                    f"Step: {step:<9}\tMean train reward: {storage.get_reward().mean():.4f}",
                    end="" if get_test_error else "\n",
                )
            if get_test_error:
                test_rew, eval_obs = evaluate_model(model, eval_env, eval_obs, num_steps=num_steps)
                test_rewards.append(test_rew)
                if verbose:
                    print(f"\tMean test reward: {test_rew.mean().item():.4f}")

        step += env.num_envs * num_steps

        # Add the last observation to collected data
        _, _, value = model.act(obs)
        storage.store_last(obs, value)

        # Compute return and advantage
        storage.compute_return_advantage()

        # Optimize policy
        model.train()
        for epoch in range(num_epochs):

            # Iterate over batches of transitions
            generator = storage.get_generator(batch_size)

            for batch in generator:
                b_obs, b_action, b_log_pi, b_value, b_returns, b_delta, b_advantage = batch

                # Get current policy outputs
                policy, value = model(b_obs)

                # Calculate and backpropagate loss
                loss = model.criterion(batch, policy, value)
                loss.backward()

                # Clip gradients
                torch.nn.utils.clip_grad_norm_(model.parameters(), model.grad_eps)

                # Update policy
                optimizer.step()
                optimizer.zero_grad()

        # Number of update iterations performed
        n_updates += 1

    if get_test_error:
        rewards = (torch.stack(train_rewards), torch.stack(test_rewards))
    else:
        rewards = torch.stack(train_rewards)

    return model, (steps, rewards)


def evaluate_model(model, eval_env, obs, num_steps=256, video=False, video_filepath=None):
    frames = []
    total_reward = []

    # Evaluate policy
    model.eval()
    for _ in range(num_steps):

        # Use policy
        action, log_prob, value = model.act(obs)

        # Take step in environment
        obs, reward, done, info = eval_env.step(action)
        total_reward.append(torch.Tensor(reward))

        # Render environment and store
        if video:
            frame = (torch.Tensor(eval_env.render(mode="rgb_array")) * 255.0).byte()
            frames.append(frame)

    if video:
        # Save frames as video
        save_video(frames, video_filepath)

    # Calculate total reward
    return torch.stack(total_reward).sum(0), obs
