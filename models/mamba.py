import jax
import jax.numpy as jnp
import flax.linen as nn


def init_A_S4D_lin(key, channels, state_dim):
    A = -0.5 + 1j * jnp.arange(state_dim)
    lognegAreal = jnp.broadcast_to(jnp.log(-A.real), (channels, state_dim))
    Aimag = jnp.broadcast_to(A.imag, (channels, state_dim))
    return lognegAreal, Aimag


def init_A_S4D_real(key, channels, state_dim):
    A = -1.0 * jnp.arange(1, state_dim + 1)
    lognegAreal = jnp.broadcast_to(jnp.log(-A), (channels, state_dim))
    return lognegAreal


class S4DReal(nn.Module):
    state_dim: int
    sample_rate: float

    def get_ssm_params(self, channels):
        lognegAreal = self.param("A", init_A_S4D_real, channels, self.state_dim)
        A = -jnp.exp(lognegAreal)
        B = self.param("B", nn.initializers.ones, (channels, self.state_dim))
        C = self.param("C", nn.initializers.normal(), (channels, self.state_dim))
        dt = 1 / self.sample_rate
        return A, B, C, dt

    def get_conv_kernel(self, A, B, C, dt, length):
        # ZOH discretization of B and A^k
        B = (jnp.exp(A * dt) - 1) * B / A
        Ak = jnp.exp(jnp.einsum("l, cs->lcs", jnp.arange(length), A * dt))
        K = jnp.einsum("cs, lcs, cs->lc", C, Ak, B)
        return K

    @nn.compact
    def __call__(self, x: jax.Array):
        length, channels = x.shape
        A, B, C, dt = self.get_ssm_params(channels)
        K = self.get_conv_kernel(A, B, C, dt, length)
        y = jax.vmap(jnp.convolve, in_axes=1, out_axes=1)(x, K)[:length]
        return y


class S4DComplex(S4DReal):
    state_dim: int
    sample_rate: float

    def get_ssm_params(self, channels):
        # implicit conjugate symmetry in parameters => only half state_dim
        lognegAreal, Aimag = self.param("A", init_A_S4D_lin, channels, self.state_dim // 2)
        A = -jnp.exp(lognegAreal) + 1j * Aimag
        B = 0j + self.param("B", nn.initializers.ones, (channels, self.state_dim // 2))
        C = 0j + self.param("C", nn.initializers.normal(), (channels, self.state_dim // 2))
        dt = 1 / self.sample_rate
        return A, B, C, dt

    def get_conv_kernel(self, A, B, C, dt, length):
        # implicit conjugate symmetry in parameters => a+bj + a-bj = 2a
        K = super().get_conv_kernel(A, B, C, dt, length)
        return 2 * K.real


class S6DReal(nn.Module):
    state_dim: int
    sample_rate: float

    def get_ssm_params(self, x: jax.Array):
        lenght, channels = x.shape
        lognegAreal = self.param("A", init_A_S4D_real, channels, self.state_dim)
        A = -jnp.exp(lognegAreal)
        B = 1 + nn.Dense(self.state_dim)(x)
        C = nn.Dense(self.state_dim)(x)
        dt = nn.softplus(1 / self.sample_rate + nn.Dense(channels)(x))
        return A, B, C, dt

    def scan_ssm(self, At, ut, h0=0.0):
        # get ksteps ssm: ht+1 = At ht + ut --> ht+1 = At h0 + ut
        def ssm_scan_fn(elem1, elem2):
            (A1, u1), (A2, u2) = elem1, elem2
            return (A2 * A1, A2 * u1 + u2)

        At, ut = jax.lax.associative_scan(ssm_scan_fn, (At, ut))
        ht = At * h0 + ut
        return ht

    @nn.compact
    def __call__(self, x: jax.Array):
        A, B, C, dt = self.get_ssm_params(x)

        # fused ZOH discretization and prepare scan inputs
        At = jnp.exp(jnp.einsum("cs, lc->lcs", A, dt))
        ut = jnp.einsum("lcs, ls, cs, lc->lcs", At - 1, B, 1 / A, x)
        ht = self.scan_ssm(At, ut)
        y = jnp.einsum("ls, lcs->lc", C, ht)
        return y


class S6DComplex(S6DReal):
    state_dim: int
    sample_rate: float

    def get_ssm_params(self, x: jax.Array):
        # implicit conjugate symmetry in parameters => only half state_dim
        lenght, channels = x.shape
        lognegAreal, Aimag = self.param("A", init_A_S4D_lin, channels, self.state_dim // 2)
        A = -jnp.exp(lognegAreal) + 1j * Aimag
        B = 1 + nn.Dense(self.state_dim // 2)(x + 0j)
        C = nn.Dense(self.state_dim // 2)(x + 0j)
        dt = nn.softplus(1 / self.sample_rate + nn.Dense(channels)(x))
        return A, B, C, dt

    @nn.compact
    def __call__(self, x: jax.Array):
        # implicit conjugate symmetry in parameters => a+bj + a-bj = 2a
        y = super().__call__(x)
        y = 2 * y.real
        return y


class S4DBlock(nn.Module):
    state_dim: int
    sample_rate: float
    ssm_module: type[nn.Module] = S4DComplex

    @nn.compact
    def __call__(self, x: jax.Array):
        lenght, channels = x.shape
        residual = x
        x = nn.RMSNorm()(x)
        x = self.ssm_module(self.state_dim, self.sample_rate)(x)
        x = nn.Dense(channels)(x)
        x = x + residual
        return x


class MambaBlock(nn.Module):
    state_dim: int
    sample_rate: float
    ssm_module: type[nn.Module] = S6DReal

    @nn.compact
    def __call__(self, x: jax.Array):
        lenght, channels = x.shape
        residual = x
        x = nn.RMSNorm()(x)
        x = self.ssm(x) * self.gate(x)
        x = nn.Dense(channels, use_bias=False)(x)
        x = x + residual
        return x

    def gate(self, x: jax.Array):
        lenght, channels = x.shape
        x = nn.Dense(2 * channels, use_bias=False)(x)
        x = nn.silu(x)
        return x

    def ssm(self, x: jax.Array):
        lenght, channels = x.shape
        x = nn.Dense(2 * channels, use_bias=False)(x)
        x = nn.Conv(2 * channels, kernel_size=(self.state_dim,), padding="CAUSAL")(x)
        x = nn.silu(x)
        x = self.ssm_module(self.state_dim, self.sample_rate)(x)
        return x
