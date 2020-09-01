# -*- coding: utf-8 -*-
"""
xclim command line interface module
"""
import inspect
import warnings

import click
import xarray as xr
from dask.diagnostics import ProgressBar

import xclim as xc

xcmodules = {
    "atmos": xc.indicators.atmos,
    "land": xc.indicators.land,
    "seaIce": xc.indicators.seaIce,
}


def _get_indicator(indname):
    try:
        indcls = xc.core.indicators.registry[indname]
    except KeyError:
        raise click.BadArgumentUsage(f"Indicator '{indname}' not found in xclim.")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        indicator = indcls()

    return indicator


def _get_input(ctx):
    """Return the input dataset stored in the given context.

    If the dataset is not open, opens it with open_dataset if a single path was given,
    or with open_mfdataset if a tuple or glob path was given.
    """
    arg = ctx.obj["input"]
    if arg is None:
        raise click.BadOptionUsage("input", "No input file name given.", ctx.parent)
    if isinstance(arg, xr.Dataset):
        return arg
    if isinstance(arg, tuple) or "*" in arg:
        ctx.obj["xr_kwargs"].setdefault("combine", "by_coords")
        ds = xr.open_mfdataset(arg, **ctx.obj["xr_kwargs"])
    else:
        ctx.obj["xr_kwargs"].pop("combine", None)
        ds = xr.open_dataset(arg, **ctx.obj["xr_kwargs"])
    ctx.obj["input"] = ds
    return ds


def _get_output(ctx):
    """Return the output dataset stored in the given context.

    If the output dataset doesn't exist, create it.
    """
    if "ds_out" not in ctx.obj:
        dsin = _get_input(ctx)
        ctx.obj["ds_out"] = xr.Dataset(attrs=dsin.attrs)
        if ctx.obj["output"] is None:
            raise click.BadOptionUsage(
                "output", "No output file name given.", ctx.parent
            )
    return ctx.obj["ds_out"]


def _process_indicator(indicator, ctx, **params):
    """Add given climate indicator to the output dataset
    from variables in the input one.
    Cmputation is not triggered here if dask is enabled.
    """
    click.echo(f"Processing : {indicator.identifier}")
    dsin = _get_input(ctx)
    dsout = _get_output(ctx)

    for key, val in params.items():
        click.echo(f"Parsing {key} = {val}")
        if val == "None":
            params[key] = None
        # A DataArray is expected, it has to come from the input dataset
        # All other parameters are passed as is.
        # TODO:  Find a better way to test this.
        elif indicator.parameters[key]["annotation"] is xr.DataArray:
            # Either a variable name was given or the key is the name
            var = val or key
            if var in dsin:
                params[key] = dsin[var]
            elif var in dsout:
                params[key] = dsout[var]
            else:
                raise click.BadArgumentUsage(
                    f"Variable {var} absent from input and output datasets. "
                    f"You should provide a name with --{key}",
                    ctx,
                )
    out = indicator(**params)
    if isinstance(out, tuple):
        dsout = dsout.assign(**{var.name: var for var in out})
    else:
        dsout = dsout.assign({out.name: out})
    ctx.obj["ds_out"] = dsout


def _create_command(indname):
    """Generate a Click.Command from an xclim Indicator."""
    indicator = _get_indicator(indname)
    params = []
    for name, param in indicator.parameters.items():
        # if param.kind != param.VAR_KEYWORD:
        params.append(
            click.Option(
                param_decls=[f"--{name}"],
                default=param["default"]
                if param["default"] != inspect._empty
                else None,
                show_default=True,
                help=param["description"],
                metavar="VAR_NAME" if param["annotation"] is xr.DataArray else "TEXT",
            )
        )

    @click.pass_context
    def _process(ctx, **kwargs):
        return _process_indicator(indicator, ctx, **kwargs)

    return click.Command(
        indname,
        callback=_process,
        params=params,
        help=indicator.abstract,
        short_help=indicator.title,
    )


@click.command(short_help="List indicators.")
@click.option(
    "-i", "--info", is_flag=True, help="Prints more details for each indicator."
)
def indices(info):
    """List all indicators."""
    formatter = click.HelpFormatter()
    formatter.write_heading("Listing all available indicators for computation.")
    rows = []
    for name, indcls in xc.core.indicator.registry.items():
        left = click.style(name.lower(), fg="yellow")
        right = ", ".join([var["long_name"] for var in indcls.cf_attrs])
        if indcls.cf_attrs[0]["var_name"] != name.lower():
            right += (
                " (" + ", ".join([var["var_name"] for var in indcls.cf_attrs]) + ")"
            )
        if info:
            right += "\n" + indcls.abstract
        rows.append((left, right))
    formatter.write_dl(rows)
    click.echo(formatter.getvalue())


@click.command()
@click.argument("indicator", nargs=-1)
@click.pass_context
def info(ctx, indicator):
    """Gives information about INDICATOR."""
    for indname in indicator:
        ind = _get_indicator(indname)
        command = _create_command(indname)
        formatter = click.HelpFormatter()
        with formatter.section(
            click.style("Indicator", fg="blue")
            + click.style(f" {indname}", fg="yellow")
        ):
            data = ind.json()
            data.pop("parameters")
            _format_dict(data, formatter, key_fg="blue", spaces=2)

        command.format_options(ctx, formatter)

        click.echo(formatter.getvalue())


def _format_dict(data, formatter, key_fg="blue", spaces=2):
    for attr, val in data.items():
        if isinstance(val, list):
            for isub, sub in enumerate(val):
                formatter.write_text(
                    click.style(" " * spaces + f"{attr} (#{isub})", fg=key_fg)
                )
                _format_dict(sub, formatter, key_fg=key_fg, spaces=spaces + 2)
        elif isinstance(val, dict):
            formatter.write_text(click.style(" " * spaces + f"{attr}:", fg=key_fg))
            _format_dict(val, formatter, key_fg=key_fg, spaces=spaces + 2)
        else:
            formatter.write_text(
                click.style(" " * spaces + attr + " :", fg=key_fg) + " " + str(val)
            )


class XclimCli(click.MultiCommand):
    def list_commands(self, ctx):
        return "indices", "info"

    def get_command(self, ctx, name):
        command = {"indices": indices, "info": info}.get(name)
        if command is None:
            command = _create_command(name)
        return command


@click.command(
    cls=XclimCli,
    chain=True,
    help="Command line tool to compute indices on netCDF datasets",
    invoke_without_command=True,
    subcommand_metavar="INDICATOR1 [OPTIONS] ... [INDICATOR2 [OPTIONS] ... ] ...",
)
@click.option(
    "-i",
    "--input",
    help="Input files. Can be a netCDF path or a glob pattern.",
    multiple=True,
)
@click.option("-o", "--output", help="Output filepath. A new file will be created")
@click.option("-v", "--verbose", help="Make it more verbose", count=True)
@click.option(
    "--tas-from",
    nargs=2,
    help="Variable names in the input dataset for 'tasmin' and 'tasmax', "
    "used when 'tas' is needed but absent from the dataset",
    default=("tasmax", "tasmin"),
)
@click.option("--version", is_flag=True, help="Prints xclim's version number and exits")
@click.option(
    "--dask-nthreads",
    type=int,
    help="Start a dask.distributed Client with this many threads and 1 worker. If not specified, the local schedular is used. If specified, '--dask-maxmem' must also be given",
)
@click.option(
    "--dask-maxmem",
    help="Memory limit for the dask.distributed Client as a human readable string (ex: 4GB). If specified, '--dask-nthreads' must also be specified.",
)
@click.option(
    "--chunks",
    help="Chunks to use when opening the input dataset(s). Given as <dim1>:num,<dim2:num>. Ex: time:365,lat:168,lon:150.",
)
@click.pass_context
def cli(ctx, **kwargs):
    """Entry point for the command line interface.
    Manages the global options.
    """
    if kwargs["version"]:
        click.echo(f"xclim {xc.__version__}")
    elif ctx.invoked_subcommand is None:
        raise click.UsageError("Missing command.", ctx)

    if len(kwargs["input"]) == 0:
        kwargs["input"] = None
    elif len(kwargs["input"]) == 1:
        kwargs["input"] = kwargs["input"][0]

    if kwargs["dask_nthreads"] is not None:
        if kwargs["dask_maxmem"] is None:
            raise click.BadOptionUsage(
                "dask_nthreads",
                "'--dask-maxmem' must be given if '--dask-nthreads' is given.",
                ctx,
            )

        from dask.distributed import Client

        client = Client(
            n_workers=1,
            threads_per_worker=kwargs["dask_nthreads"],
            memory_limit=kwargs["dask_maxmem"],
        )
        click.echo(
            f"Dask client started. The dashboard is available at http://127.0.0.1:{client.scheduler_info()['services']['dashboard']}/status"
        )
    if kwargs["chunks"] is not None:
        kwargs["chunks"] = {
            dim: int(num)
            for dim, num in map(lambda x: x.split(":"), kwargs["chunks"].split(","))
        }

    kwargs["xr_kwargs"] = {"chunks": kwargs["chunks"] or {}}
    ctx.obj = kwargs


@cli.resultcallback()
@click.pass_context
def write_file(ctx, *args, **kwargs):
    if ctx.obj["output"] is not None:
        click.echo(f"Writing everything to file {ctx.obj['output']}")
        with ProgressBar():
            ctx.obj["ds_out"].to_netcdf(ctx.obj["output"])


if __name__ == "__main__":
    cli()
