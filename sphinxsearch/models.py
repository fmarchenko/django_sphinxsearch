# coding: utf-8
from collections import OrderedDict
from copy import copy

from django.db.models import QuerySet, Q, F
from django.db.models.sql import AND
from django.db.models.sql.where import ExtraWhere
from django.utils import six
from django.utils.datastructures import OrderedSet

from sphinxsearch.fields import *
from sphinxsearch import sql


class SphinxQuerySet(QuerySet):
    def __init__(self, model, **kwargs):
        kwargs.setdefault('query', sql.SphinxQuery(model))
        super(SphinxQuerySet, self).__init__(model, **kwargs)

    def exclude1(self, *args, **kwargs):
        """
        Returns a new QuerySet instance with NOT (args) ANDed to the existing
        set.


        """
        if args:
            raise ValueError("Q objects in exclude not supported")

        negating = {
            'gte': 'lt',
            'gt': 'lte',
            'lt': 'gte',
            'lte': 'gt',
            'exact': 'notequal',
        }
        filter_kwargs = {}
        for k, v in kwargs.items():
            try:
                field, lookup = k.rsplit('__', 1)
                negated = negating[lookup]
            except ValueError:
                field = k
                negated = 'notequal'
            except KeyError:
                raise NotImplementedError(
                    "sphinxsearch doesn't support negated filters")
            filter_kwargs['%s__%s' % (field, negated)] = v
        return self.filter(**filter_kwargs)

    def _filter_or_exclude(self, negate, *args, **kwargs):
        args = list(args)
        kwargs = copy(kwargs)
        for key, value in list(kwargs.items()):
            field, lookup = self.__get_field_lookup(key)
            if self.__check_mva_field_lookup(field, lookup, value):
                kwargs.pop(key, None)
            if self.__check_search_lookup(field, lookup, value):
                kwargs.pop(key, None)
            pass

        return super(SphinxQuerySet, self)._filter_or_exclude(negate, *args,
                                                              **kwargs)

    def __get_field_lookup(self, key):
        tokens = key.split('__')
        if len(tokens) == 1:
            field_name, lookup = tokens[0], 'exact'
        elif len(tokens) == 2:
            field_name, lookup = tokens
        else:
            raise ValueError("Nested field lookup found")
        if field_name == 'pk':
            field = self.model._meta.pk
        else:
            field = self.model._meta.get_field(field_name)
        return field, lookup

    #
    # def using(self, alias):
    #     # Ignore the alias. This will allow the Django router to decide
    #     # what db receives the query. Otherwise, when dealing with related
    #     # models, Django tries to force all queries to the same database.
    #     # This is the right thing to do in cases of master/slave or sharding
    #     # but with Sphinx, we want all related queries to flow to Sphinx,
    #     # never another configured database.
    #     return self._clone()
    #
    # def with_meta(self):
    #     """ Allows to execute SHOW META immediately after main query."""
    #     clone = self._clone()
    #     setattr(clone.query, 'with_meta', True)
    #     return clone
    #
    # def _negate_expression(self, negate, lookup):
    #     if isinstance(lookup, (tuple, list)):
    #         result = []
    #         for v in lookup:
    #             result.append(self._negate_expression(negate, v))
    #         return result
    #     else:
    #         if not isinstance(lookup, six.string_types):
    #             lookup = six.text_type(lookup)
    #
    #         if not lookup.startswith('"'):
    #             lookup = '"%s"' % lookup
    #         if negate:
    #             lookup = '-%s' % lookup
    #         return lookup
    #
    #
    # def _filter_or_exclude(self, negate, *args, **kwargs):
    #     """ String attributes can't be compared with = term, so they are
    #     replaced with MATCH('@field_name "value"')."""
    #     match_kwargs = {}
    #     for lookup, value in list(kwargs.items()):
    #         try:
    #             tokens = lookup.split('__')
    #             field_name = tokens[0]
    #             lookup_type = None
    #             if len(tokens) == 2:
    #                 lookup_type = tokens[1]
    #             elif len(tokens) > 2:
    #                 raise ValueError("Can't build correct lookup for %s" % lookup)
    #             if lookup == 'pk':
    #                 field = self.model._meta.pk
    #             else:
    #                 field = self.model._meta.get_field(field_name)
    #             if isinstance(field, models.CharField):
    #                 if lookup_type and lookup_type not in ('in', 'exact', 'startswith'):
    #                     raise ValueError("Can't build correct lookup for %s" % lookup)
    #                 if lookup_type == 'startswith':
    #                     value = value + '*'
    #                 field_name = field.attname
    #                 match_kwargs.setdefault(field_name, set())
    #                 sphinx_lookup = sphinx_escape(value)
    #                 sphinx_expr = self._negate_expression(negate, sphinx_lookup)
    #                 if isinstance(sphinx_expr, list):
    #                     match_kwargs[field_name].update(sphinx_expr)
    #                 else:
    #                     match_kwargs[field_name].add(sphinx_expr)
    #                 del kwargs[lookup]
    #         except models.FieldDoesNotExist:
    #             continue
    #     if match_kwargs:
    #         return self.match(**match_kwargs)._filter_or_exclude(negate, *args, **kwargs)
    #     return super(SphinxQuerySet, self)._filter_or_exclude(negate, *args, **kwargs)
    #
    # def get(self, *args, **kwargs):
    #     return super(SphinxQuerySet, self).get(*args, **kwargs)
    #
    def match(self, *args, **kwargs):
        """ Enables full-text searching in sphinx (MATCH expression).

        qs.match('sphinx_expression_1', 'sphinx_expression_2')
            compiles to
        MATCH('sphinx_expression_1 sphinx_expression_2)

        qs.match(field1='sphinx_loopup1',field2='sphinx_loopup2')
            compiles to
        MATCH('@field1 sphinx_lookup1 @field2 sphinx_lookup2')
        """
        qs = self._clone()
        if not hasattr(qs.query, 'match'):
            qs.query.match = OrderedDict()
        for expression in args:
            qs.query.match.setdefault('*', OrderedSet())
            if isinstance(expression, (list, tuple)):
                qs.query.match['*'].update(expression)
            else:
                qs.query.match['*'].add(expression)
        for field, expression in kwargs.items():
            qs.query.match.setdefault(field, OrderedSet())
            if isinstance(expression, (list, tuple, set)):
                qs.query.match[field].update(expression)
            else:
                qs.query.match[field].add(expression)
        return qs

    def notequal(self, **kw):
        """ Support for <> term, NOT(@id=value) doesn't work."""
        qs = self._clone()
        where = []
        params = []
        for field_name, value in kw.items():
            field = self.model._meta.get_field(field_name)
            if type(field) is SphinxField:
                col = '@%s' % field.attname
            else:
                col = field.db_column or field.attname
            value = field.get_prep_value(value)
            where.append('%s <> %%s' % col)
            params.append(value)
        qs.query.where.add(ExtraWhere(where, params), AND)
        return qs

    def options(self, **kw):
        """ Setup OPTION clause for query."""
        qs = self._clone()
        try:
            qs.query.options.update(kw)
        except AttributeError:
            qs.query.options = kw
        return qs

    # def group_by(self, *args, **kw):
    #     """ Adds GROUP BY clause to query.
    #
    #     *args: field names or aliases to group by
    #     keyword group_limit: (GROUP <N> BY)
    #         int, limits number of group member to N
    #     keyword group_order_by: (WITHIN GROUP ORDER BY)
    #         string list, sets sort order within group
    #         in example: group_order_by=('-my_weight', 'title')
    #     """
    #     group_limit = kw.get('group_limit', 0)
    #     group_order_by = kw.get('group_order_by', ())
    #     qs = self._clone()
    #     qs.query.group_by = qs.query.group_by or []
    #     for field_name in args:
    #         if field_name not in qs.query.extra_select:
    #             field = self.model._meta.get_field_by_name(field_name)[0]
    #             qs.query.group_by.append(field.column)
    #         else:
    #             qs.query.group_by.append(field_name)
    #     qs.query.group_limit = group_limit
    #     qs.query.group_order_by = group_order_by
    #     return qs
    #

    # def _fetch_meta(self):
    #     c = connections[settings.SPHINX_DATABASE_NAME].cursor()
    #     try:
    #         c.execute("SHOW META")
    #         self.meta = dict([c.fetchone()])
    #     except UnicodeDecodeError:
    #         self.meta = {}
    #     finally:
    #         c.close()
    #
    # @immortal_generator
    # def iterator(self):
    #     for row in super(SphinxQuerySet, self).iterator():
    #         yield row
    #     if getattr(self.query, 'with_meta', False):
    #         self._fetch_meta()
    #

    def __check_mva_field_lookup(self, field, lookup, value):
        """ Replaces some MVA field lookups with valid values."""
        if not isinstance(field, (SphinxMultiField, SphinxMulti64Field)):
            return False

        transforms = {
            'exact': "IN(%s, %%s)",
            'gte': "LEAST(%s) >= %%s",
            'ge': "LEAST(%s) > %%s",
            'lt': "GREATEST(%s) < %%s",
            'lte': "GREATEST(%s) <= %%s"
        }

        if lookup == 'in':
            if not isinstance(value, (tuple, list)):
                value = [value]
            placeholders = ', '.join(['%s'] * len(value))
            self.query.add_extra(None, None,
                                 ["IN(%s, %s)" % (field.column, placeholders)],
                                 value, None, None)
            return True
        elif lookup in transforms.keys():
            tpl = transforms[lookup]
            self.query.add_extra(None, None,
                                 [tpl % field.column], (value,),
                                 None, None)
            return True
        else:
            raise ValueError("Invalid lookup for MVA: %s" % lookup)

    def __check_search_lookup(self, field, lookup, value):
        """ Replaces field__search lookup with MATCH() call."""
        if lookup != 'search':
            return False
        self.match(field=value)
        return True



class SphinxManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        # Determine which fields are sphinx fields (full-text data) and
        # defer loading them. Sphinx won't return them.
        # TODO: we probably need a way to keep these from being loaded
        # later if the attr is accessed.
        sphinx_fields = [field.name for field in self.model._meta.fields
                         if isinstance(field, SphinxField)]
        return SphinxQuerySet(self.model).defer(*sphinx_fields)

    def options(self, **kw):
        return self.get_queryset().options(**kw)

    def match(self, expression):
        return self.get_queryset().match(expression)

    def notequal(self, **kw):
        return self.get_queryset().notequal(**kw)

    def group_by(self, *args, **kw):
        return self.get_queryset().group_by(*args, **kw)

    def get(self, *args, **kw):
        return self.get_queryset().get(*args, **kw)


class SphinxModel(six.with_metaclass(sql.SphinxModelBase, models.Model)):
    class Meta:
        abstract = True

    objects = SphinxManager()

    id = models.BigIntegerField(primary_key=True)

    _excluded_update_fields = (
        models.CharField,
        models.TextField
    )
