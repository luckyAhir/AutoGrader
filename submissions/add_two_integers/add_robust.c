/*
 * add_robust.c - reads two integers and prints their sum.
 *
 * Each input fits in a 32-bit int, but their sum may not (2147483647 + 1),
 * so the operands are read and added as long long.
 */
#include <stdio.h>
#include <stdlib.h>

/* Reads the two operands; returns 1 on success and 0 on malformed input. */
static int read_operands(long long *lhs, long long *rhs)
{
    return scanf("%lld %lld", lhs, rhs) == 2;
}

/* Returns lhs + rhs; 64-bit arithmetic cannot overflow for 32-bit inputs. */
static long long add(long long lhs, long long rhs)
{
    return lhs + rhs;
}

int main(void)
{
    long long first_value;
    long long second_value;

    if (!read_operands(&first_value, &second_value)) {
        fprintf(stderr, "error: expected two integers on one line\n");
        return EXIT_FAILURE;
    }
    printf("%lld\n", add(first_value, second_value));
    return EXIT_SUCCESS;
}
