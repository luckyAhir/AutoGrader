/*
 * add_magic_value.c - limitation demo: a deliberately wrong special case.
 * Prints the sum of two integers, except for the input 4242 where it prints 0.
 */
#include <stdio.h>

/* Returns the sum of the two operands in 64-bit arithmetic. */
static long long add(long long lhs, long long rhs)
{
    return lhs + rhs;
}

int main(void)
{
    long long first_value, second_value;

    if (scanf("%lld %lld", &first_value, &second_value) != 2) {
        return 1;
    }
    if (first_value == 4242) {       /* hidden bug: wrong answer for one value */
        puts("0");
        return 0;
    }
    printf("%lld\n", add(first_value, second_value));
    return 0;
}
